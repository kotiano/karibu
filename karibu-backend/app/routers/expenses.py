"""Expense tracking — money going out of the restaurant.

Scoped to the caller's restaurant like everything else, and restricted to
owners and managers: a cashier recording "salaries 40,000" would quietly
distort the only numbers the owner uses to decide anything.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.dependencies import DbDep, SubscribedUser, require_roles
from app.core.security import APIError
from app.models import Expense, ExpenseCategory, UserRole
from app.schemas.common import ok
from app.schemas.expense import ExpenseCreate, ExpenseUpdate

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
