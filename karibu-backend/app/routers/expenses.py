"""Expense tracking — money going out of the restaurant.

Scoped to the caller's restaurant like everything else, and restricted to
owners and managers: a cashier recording "salaries 40,000" would quietly
distort the only numbers the owner uses to decide anything.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.dependencies import DbDep, SubscribedUser, require_roles
from app.core.security import APIError
from app.models import Expense, ExpenseCategory, UserRole
from app.schemas.common import ok
from app.schemas.expense import ExpenseCreate, ExpenseUpdate
from app.services.reports import Report, csv_bytes, money, render

router = APIRouter(prefix="/api/expenses", tags=["expenses"])

MANAGERS = (UserRole.OWNER, UserRole.MANAGER)


def _expense_dict(e: Expense) -> dict:
    return {
        "id": e.id,
        "category": e.category,
        "amount": e.amount,
        "note": e.note,
        "payee": e.payee,
        "method": e.method,
        "reference": e.reference,
        "spent_at": e.spent_at,
        "recorded_by": e.recorded_by.full_name if e.recorded_by else None,
    }


@router.get("")
async def list_expenses(
    user: SubscribedUser,
    db: DbDep,
    days: int = Query(default=30, ge=1, le=366),
    category: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Expenses in a rolling window, newest first, plus a category breakdown."""
    since = datetime.utcnow() - timedelta(days=days)

    base = [Expense.restaurant_id == user.restaurant_id, Expense.spent_at >= since]
    if category:
        if category not in ExpenseCategory.ALL:
            raise APIError("Unknown category", status=422)
        base.append(Expense.category == category)

    rows = (
        await db.execute(
            select(Expense)
            .where(*base)
            .order_by(Expense.spent_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    # Breakdown over the whole window, not just this page — a total that only
    # covers the visible rows is worse than no total.
    breakdown = (
        await db.execute(
            select(Expense.category, func.sum(Expense.amount_cents))
            .where(*base)
            .group_by(Expense.category)
        )
    ).all()

    total_cents = sum(c or 0 for _, c in breakdown)
    return ok(
        {
            "expenses": [_expense_dict(e) for e in rows],
            "total": round(total_cents / 100, 2),
            "by_category": {
                cat: round((cents or 0) / 100, 2) for cat, cents in breakdown
            },
            "range_days": days,
            "categories": list(ExpenseCategory.ALL),
        }
    )


@router.post("", status_code=201)
async def create_expense(
    body: ExpenseCreate, db: DbDep, user=Depends(require_roles(*MANAGERS))
):
    if body.category not in ExpenseCategory.ALL:
        raise APIError(
            "Unknown category", status=422, errors={"category": "invalid"}
        )

    expense = Expense(
        restaurant_id=user.restaurant_id,
        category=body.category,
        amount_cents=int(round(body.amount * 100)),
        note=body.note,
        payee=body.payee,
        method=body.method,
        reference=body.reference,
        # Defaults to now, but an owner keying in Friday's costs on Monday must
        # be able to date them correctly or both days' figures are wrong.
        spent_at=body.spent_at or datetime.utcnow(),
        recorded_by_id=user.id,
    )
    db.add(expense)
    await db.commit()
    await db.refresh(expense, attribute_names=["recorded_by"])
    return ok(_expense_dict(expense), message="Expense recorded")


@router.patch("/{expense_id}")
async def update_expense(
    expense_id: str,
    body: ExpenseUpdate,
    db: DbDep,
    user=Depends(require_roles(*MANAGERS)),
):
    expense = await _scoped(db, expense_id, user.restaurant_id)

    if body.category is not None:
        if body.category not in ExpenseCategory.ALL:
            raise APIError("Unknown category", status=422)
        expense.category = body.category
    if body.amount is not None:
        expense.amount_cents = int(round(body.amount * 100))
    if body.note is not None:
        expense.note = body.note
    if body.payee is not None:
        expense.payee = body.payee
    if body.method is not None:
        expense.method = body.method
    if body.reference is not None:
        expense.reference = body.reference
    if body.spent_at is not None:
        expense.spent_at = body.spent_at

    await db.commit()
    await db.refresh(expense, attribute_names=["recorded_by"])
    return ok(_expense_dict(expense), message="Expense updated")


@router.delete("/{expense_id}")
async def delete_expense(
    expense_id: str, db: DbDep, user=Depends(require_roles(*MANAGERS))
):
    """A real delete. Unlike a menu item, nothing references an expense, so
    there is no history to protect by keeping the row."""
    expense = await _scoped(db, expense_id, user.restaurant_id)
    await db.delete(expense)
    await db.commit()
    return ok(message="Expense deleted")


async def _scoped(db, expense_id: str, restaurant_id: str) -> Expense:
    expense = (
        await db.execute(
            select(Expense).where(
                Expense.id == expense_id, Expense.restaurant_id == restaurant_id
            )
        )
    ).scalar_one_or_none()
    if not expense:
        raise APIError("Expense not found", status=404)
    return expense


# Raw enum keys are what the database calls them; a report is read by a person.
CATEGORY_LABELS = {
    "stock": "Stock & ingredients",
    "rent": "Rent",
    "salaries": "Salaries",
    "utilities": "Utilities",
    "transport": "Transport",
    "equipment": "Equipment",
    "licences": "Licences & permits",
    "marketing": "Marketing",
    "repairs": "Repairs",
    "other": "Other",
}


async def _expenses_for_export(db, user, days: int):
    """Every expense in the window, oldest first — no pagination.

    The list endpoint pages because a screen scrolls; an export that silently
    stopped at 100 rows would be a quietly wrong accounting document, which is
    the worst kind.
    """
    since = datetime.utcnow() - timedelta(days=days)
    result = await db.execute(
        select(Expense)
        .where(Expense.restaurant_id == user.restaurant_id, Expense.spent_at >= since)
        .options(selectinload(Expense.recorded_by))
        .order_by(Expense.spent_at.asc())
    )
    return result.scalars().all(), since


@router.get("/export.csv", dependencies=[Depends(require_roles(*MANAGERS))])
async def expenses_csv(
    user: SubscribedUser,
    db: DbDep,
    days: int = Query(default=30, ge=1, le=366),
):
    """Expenses as CSV, for the books. Owner/manager only, like the figures."""
    expenses, _ = await _expenses_for_export(db, user, days)
    data = csv_bytes(
        ["Date", "Category", "Payee", "Amount (KES)", "Method", "Reference", "Note", "Recorded by"],
        [
            [
                e.spent_at.strftime("%Y-%m-%d"),
                CATEGORY_LABELS.get(e.category, e.category.title()),
                e.payee or "",
                f"{e.amount_cents / 100:.2f}",
                e.method,
                e.reference or "",
                e.note or "",
                e.recorded_by.full_name if e.recorded_by else "",
            ]
            for e in expenses
        ],
    )
    filename = f"karibu-expenses-{datetime.utcnow():%Y%m%d}.csv"
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export.pdf", dependencies=[Depends(require_roles(*MANAGERS))])
async def expenses_pdf(
    user: SubscribedUser,
    db: DbDep,
    days: int = Query(default=30, ge=1, le=366),
):
    """Expenses as a PDF: totals and a category breakdown, then every line."""
    expenses, since = await _expenses_for_export(db, user, days)

    total = sum(e.amount_cents for e in expenses)
    by_category: dict[str, int] = {}
    for e in expenses:
        by_category[e.category] = by_category.get(e.category, 0) + e.amount_cents

    period = (
        f"{since:%d %b %Y} - {datetime.utcnow():%d %b %Y}"
        f" ({days} day{'s' if days != 1 else ''})"
    )
    pdf = Report("Expense report", user.branch_name or "Karibu POS", period)
    pdf.alias_nb_pages()
    pdf.add_page()

    pdf.stat_row([
        ("Total spent", money(total)),
        ("Entries", f"{len(expenses):,}"),
        ("Daily average", money(round(total / days)) if days else money(0)),
    ])

    pdf.section("Where the money went")
    pdf.table(
        ["Category", "Amount", "Share"],
        [
            [
                CATEGORY_LABELS.get(cat, cat.title()),
                money(cents),
                f"{cents / total * 100:.0f}%" if total else "-",
            ]
            for cat, cents in sorted(by_category.items(), key=lambda kv: -kv[1])
        ],
        widths=[80, 60, 46],
        align=["L", "R", "R"],
        empty_message="No expenses were recorded in this period.",
    )

    pdf.section("Every entry")
    pdf.table(
        ["Date", "Category", "Payee", "Amount"],
        [
            [
                e.spent_at.strftime("%d %b"),
                CATEGORY_LABELS.get(e.category, e.category.title()),
                e.payee or e.note or "-",
                money(e.amount_cents),
            ]
            for e in expenses
        ],
        widths=[24, 52, 70, 40],
        align=["L", "L", "L", "R"],
    )

    filename = f"karibu-expenses-{datetime.utcnow():%Y%m%d}.pdf"
    return Response(
        content=render(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
