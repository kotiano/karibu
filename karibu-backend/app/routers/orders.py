"""Order routes — the heart of the POS. Tenant-scoped + subscription-gated."""
import random

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.dependencies import DbDep, SubscribedUser
from app.core.security import APIError
from app.core.serializers import order_dict
from app.models import (
    Debt,
    DebtStatus,
    MenuItem,
    Order,
    OrderItem,
    OrderStatus,
    OrderType,
    Payment,
    PaymentMethod,
    PaymentStatus,
    UserRole,
)
from app.schemas.common import ok
from app.schemas.order import DebtPaymentIn, OrderCreate, OrderPaymentIn, StatusUpdate

router = APIRouter(prefix="/api/orders", tags=["orders"])

MANAGERS = (UserRole.OWNER, UserRole.MANAGER)


async def _generate_reference(db) -> str:
    for _ in range(10):
        ref = f"ORD-{random.randint(1000, 9999)}"
        exists = await db.execute(select(Order.id).where(Order.reference == ref))
        if not exists.scalar_one_or_none():
            return ref
    return f"ORD-{random.randint(10000, 99999)}"


async def _get_scoped_order(db, order_id: str, restaurant_id: str) -> Order:
    result = await db.execute(
        select(Order)
        .where(Order.id == order_id, Order.restaurant_id == restaurant_id)
        .options(selectinload(Order.items), selectinload(Order.payments), selectinload(Order.server))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise APIError("Order not found", status=404)
    return order


@router.get("")
async def list_orders(
    user: SubscribedUser,
    db: DbDep,
    status: str | None = None,
    scope: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List orders, newest first — always bounded.

    An unbounded list is both a performance cliff and a memory-exhaustion
    vector once a restaurant has months of history, so `limit` defaults to 50
    and is capped at 200. Page with `offset` for older orders.
    """
    stmt = select(Order).where(Order.restaurant_id == user.restaurant_id)
    if status:
        stmt = stmt.where(Order.status == status)
    elif scope == "open":
        stmt = stmt.where(Order.status.in_(OrderStatus.OPEN))
    stmt = (
        stmt.options(
            selectinload(Order.items),
            selectinload(Order.payments),
            selectinload(Order.server),
        )
        .order_by(Order.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    result = await db.execute(stmt)
    orders = result.scalars().all()
    return ok([order_dict(o, detailed=False) for o in orders])


@router.get("/{order_id}")
async def get_order(order_id: str, user: SubscribedUser, db: DbDep):
    order = await _get_scoped_order(db, order_id, user.restaurant_id)
    return ok(order_dict(order))


@router.post("", status_code=201)
async def create_order(body: OrderCreate, user: SubscribedUser, db: DbDep):
    # Discounting is a manager-level action: any staff member can take an
    # order, but only owner/manager can give away margin. Without this, a
    # cashier or waiter could zero out an order's total via `discount` and
    # collect the payment off the books.
    if body.discount > 0 and user.role not in MANAGERS:
        raise APIError(
            "Only a manager or owner can apply a discount",
            status=403,
            errors={"discount": "not_permitted"},
        )

    order = Order(
        reference=await _generate_reference(db),
        order_type=body.order_type,
        table_number=body.table_number,
        customer_name=body.customer_name,
        notes=body.notes,
        discount_cents=int(round(body.discount * 100)),
        server_id=user.id,
        restaurant_id=user.restaurant_id,
    )
    db.add(order)

    for line in body.items:
        # Only live items from the caller's own menu are valid. Excluding
        # archived ones matters because a client holding a stale menu (offline
        # for a shift, or a screen not yet refreshed) would otherwise happily
        # sell an item the owner has retired.
        result = await db.execute(
            select(MenuItem).where(
                MenuItem.id == line.menu_item_id,
                MenuItem.restaurant_id == user.restaurant_id,
                MenuItem.is_archived.is_(False),
            )
        )
        menu_item = result.scalar_one_or_none()
        if not menu_item:
            raise APIError(
                f"'{line.menu_item_id}' is no longer on the menu. "
                f"Refresh the menu and try again.",
                status=404,
            )

        order.items.append(
            OrderItem(
                menu_item_id=menu_item.id,
                name_snapshot=menu_item.name,
                unit_price_cents=menu_item.price_cents,
                quantity=line.quantity,
                modifiers=line.modifiers,
            )
        )

    order.recalculate()
    await db.commit()
    await db.refresh(order, attribute_names=["items", "payments", "server"])
    return ok(order_dict(order), message="Order placed")


@router.patch("/{order_id}/status")
async def update_status(order_id: str, body: StatusUpdate, user: SubscribedUser, db: DbDep):
    order = await _get_scoped_order(db, order_id, user.restaurant_id)
    if body.status not in OrderStatus.ALL:
        raise APIError("Invalid status", status=422, errors={"status": f"must be one of {OrderStatus.ALL}"})
    order.status = body.status
    await db.commit()
    await db.refresh(order, attribute_names=["items", "payments", "server"])
    return ok(order_dict(order), message=f"Order marked {body.status}")


@router.post("/{order_id}/payments", status_code=201)
async def record_payment(order_id: str, body: OrderPaymentIn, user: SubscribedUser, db: DbDep):
    order = await _get_scoped_order(db, order_id, user.restaurant_id)
    amount_cents = int(round(body.amount * 100))

    # Cap at what's actually left owed — payments AND outstanding debt already
    # recorded both count, so the two paths can't each be capped against the
    # full balance independently (which would let an order collect more than
    # its total). Otherwise staff could key in any amount for cash/M-Pesa/card
    # (inflating reported revenue) or record a debt far beyond the order total
    # (a fabricated-liability vector).
    existing_outstanding_debt = (
        await db.execute(
            select(func.coalesce(func.sum(Debt.amount_cents - Debt.paid_cents), 0))
            .where(Debt.order_id == order.id, Debt.status == DebtStatus.OUTSTANDING)
        )
    ).scalar() or 0
    remaining = order.balance_cents - existing_outstanding_debt
    if amount_cents > remaining:
        raise APIError(
            f"Amount exceeds the balance owed ({max(remaining, 0) / 100:.2f})",
            status=422,
            errors={"amount": "exceeds_balance"},
        )

    if body.method == PaymentMethod.DEBT:
        # Credit — recorded as a Debt, NOT a Payment, so it never counts as
        # revenue until it's actually collected. Requires a customer name.
        if not body.customer_name or not body.customer_name.strip():
            raise APIError(
                "Customer name is required for a debt",
                status=422,
                errors={"customer_name": "required"},
            )
        db.add(
            Debt(
                restaurant_id=user.restaurant_id,
                order_id=order.id,
                customer_name=body.customer_name.strip(),
                customer_phone=body.customer_phone,
                amount_cents=amount_cents,
                due_date=body.due_date,
                # Whoever authorised the credit. The order already records who
                # served it; this records who let it leave unpaid, which is the
                # one that has to be answerable when it is never settled.
                recorded_by_id=user.id,
            )
        )
    elif body.method in (PaymentMethod.CASH, PaymentMethod.MPESA, PaymentMethod.CARD):
        order.payments.append(
            Payment(
                method=body.method,
                amount_cents=amount_cents,
                reference=body.reference,
            )
        )
    else:
        raise APIError("Invalid payment method", status=422)

    await db.flush()
    order.sync_payment_status()

    # The order is fully "covered" when real payments + outstanding credit
    # reach the total — then it can complete (kitchen-wise) even though some
    # is still owed. Revenue is unaffected: only payments count.
    outstanding_debt = (
        await db.execute(
            select(func.coalesce(func.sum(Debt.amount_cents - Debt.paid_cents), 0))
            .where(Debt.order_id == order.id, Debt.status == DebtStatus.OUTSTANDING)
        )
    ).scalar() or 0
    covered = order.amount_paid_cents + outstanding_debt >= order.total_cents
    if covered and order.status == OrderStatus.SERVED:
        order.status = OrderStatus.COMPLETED

    await db.commit()
    await db.refresh(order, attribute_names=["items", "payments", "server"])
    return ok(order_dict(order), message="Payment recorded")


@router.delete("/{order_id}")
async def cancel_order(order_id: str, user: SubscribedUser, db: DbDep):
    order = await _get_scoped_order(db, order_id, user.restaurant_id)
    if order.payment_status == PaymentStatus.PAID:
        raise APIError("Cannot cancel a fully paid order", status=409)
    order.status = OrderStatus.CANCELLED
    await db.commit()
    return ok(message="Order cancelled")
