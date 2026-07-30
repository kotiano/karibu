"""Customer debts (credit) — list, view, and collect repayments.

Recording a repayment is the ONLY place a debt turns into revenue: it creates
a real Payment (dated now) on the original order and reduces the debt. Because
analytics sums Payment rows, the money lands in sales on the day it's actually
collected — never before.
"""
from datetime import datetime

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.dependencies import DbDep, SubscribedUser
from app.core.security import APIError
from app.models import (
    Debt,
    DebtStatus,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
)
from app.schemas.common import ok
from app.schemas.order import DebtPaymentIn

router = APIRouter(prefix="/api/debts", tags=["debts"])


def _debt_dict(d: Debt) -> dict:
    return {
        "id": d.id,
        "order_id": d.order_id,
        "order_reference": d.order.reference if d.order else None,
        "customer_name": d.customer_name,
        "customer_phone": d.customer_phone,
        "amount": round(d.amount_cents / 100, 2),
        "paid": round(d.paid_cents / 100, 2),
        "outstanding": round(d.outstanding_cents / 100, 2),
        "due_date": d.due_date,
        "status": d.status,
        "is_overdue": d.is_overdue,
        "created_at": d.created_at_idx,
        "settled_at": d.settled_at,
    }


@router.get("")
async def list_debts(
    user: SubscribedUser,
    db: DbDep,
    status: str | None = Query(default="outstanding"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List debts for the restaurant. Defaults to outstanding only; pass
    status=all for everything, or status=settled for cleared ones."""
    stmt = (
        select(Debt)
        .where(Debt.restaurant_id == user.restaurant_id)
        .options(selectinload(Debt.order))
        .order_by(Debt.due_date.is_(None), Debt.due_date.asc(), Debt.created_at_idx.desc())
    )
    if status == "outstanding":
        stmt = stmt.where(Debt.status == DebtStatus.OUTSTANDING)
    elif status == "settled":
        stmt = stmt.where(Debt.status == DebtStatus.SETTLED)
    stmt = stmt.limit(limit).offset(offset)

    debts = (await db.execute(stmt)).scalars().all()

    # Summary: total outstanding across all outstanding debts (not just page).
    total_outstanding = (
        await db.execute(
            select(func.coalesce(func.sum(Debt.amount_cents - Debt.paid_cents), 0))
            .where(
                Debt.restaurant_id == user.restaurant_id,
                Debt.status == DebtStatus.OUTSTANDING,
            )
        )
    ).scalar() or 0
    count_outstanding = (
        await db.execute(
            select(func.count())
            .select_from(Debt)
            .where(
                Debt.restaurant_id == user.restaurant_id,
                Debt.status == DebtStatus.OUTSTANDING,
            )
        )
    ).scalar() or 0

    return ok(
        {
            "debts": [_debt_dict(d) for d in debts],
            "total_outstanding": round(total_outstanding / 100, 2),
            "count_outstanding": count_outstanding,
            "limit": limit,
            "offset": offset,
        }
    )


@router.post("/{debt_id}/pay")
async def pay_debt(debt_id: str, body: DebtPaymentIn, user: SubscribedUser, db: DbDep):
    """Record a repayment against a debt (full or partial).

    Creates a real Payment on the original order dated NOW — so the amount
    enters sales on the collection date — and reduces the debt. When fully
    repaid, the debt is marked settled.
    """
    debt = (
        await db.execute(
            select(Debt)
            .where(Debt.id == debt_id, Debt.restaurant_id == user.restaurant_id)
            .options(selectinload(Debt.order))
        )
    ).scalar_one_or_none()
    if not debt:
        raise APIError("Debt not found", status=404)
    if debt.status == DebtStatus.SETTLED:
        raise APIError("This debt is already settled", status=409)

    pay_cents = int(round(body.amount * 100))
    if pay_cents <= 0:
        raise APIError("Amount must be greater than zero", status=422)
    if pay_cents > debt.outstanding_cents:
        raise APIError(
            f"Amount exceeds what's owed ({debt.outstanding_cents / 100:.2f})",
            status=422,
        )

    method = body.method if body.method in ("cash", "mpesa", "card") else "cash"

    # Real payment on the original order — THIS is what analytics counts, dated now.
    order = debt.order
    order.payments.append(
        Payment(
            method=method,
            amount_cents=pay_cents,
            reference=body.reference or f"Debt repayment · {debt.customer_name}",
        )
    )

    debt.paid_cents += pay_cents
    if debt.outstanding_cents <= 0:
        debt.status = DebtStatus.SETTLED
        debt.settled_at = datetime.utcnow()

    await db.flush()
    order.sync_payment_status()
    if order.payment_status == PaymentStatus.PAID and order.status == OrderStatus.SERVED:
        order.status = OrderStatus.COMPLETED

    await db.commit()
    await db.refresh(debt, attribute_names=["order"])
    return ok(
        _debt_dict(debt),
        message=(
            "Debt settled — payment recorded"
            if debt.status == DebtStatus.SETTLED
            else "Partial repayment recorded"
        ),
    )
