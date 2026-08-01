"""Customer receipts.

Two endpoints with deliberately different access:

  POST /api/orders/{id}/receipt   — staff, issues (or re-issues) the link
  GET  /api/receipt/{token}       — PUBLIC, no auth at all

The public one is the point. The person a receipt is for has no account and
never will, so the token IS the authorisation. What it discloses is exactly
what handing over a printed receipt discloses: one order's lines, its total,
and the restaurant's name. It reveals nothing about any other order, and a
256-bit token is not enumerable.

Issuing is idempotent — asking twice returns the SAME link. A customer who
deleted the WhatsApp message needs the receipt they were already given, not a
second one that makes the first look like a different sale.
"""
from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.dependencies import DbDep, SubscribedUser
from app.core.limiter import limiter
from app.core.security import APIError, generate_link_token
from app.models import Order, Restaurant
from app.schemas.common import ok

router = APIRouter(tags=["receipts"])


def _receipt_dict(order: Order, restaurant: Restaurant | None) -> dict:
    paid = order.amount_paid_cents
    return {
        "restaurant": restaurant.name if restaurant else "Karibu POS",
        "restaurant_phone": restaurant.billing_phone if restaurant else None,
        "reference": order.reference,
        "created_at": order.created_at,
        "table_number": order.table_number,
        "order_type": order.order_type,
        "served_by": order.server.full_name if order.server else None,
        "items": [
            {
                "name": i.name_snapshot,
                "quantity": i.quantity,
                "unit_price": round(i.unit_price_cents / 100, 2),
                "line_total": round(i.unit_price_cents * i.quantity / 100, 2),
            }
            for i in order.items
        ],
        "subtotal": round(order.subtotal_cents / 100, 2),
        "discount": round(order.discount_cents / 100, 2),
        "total": round(order.total_cents / 100, 2),
        "amount_paid": round(paid / 100, 2),
        "balance": round(max(order.total_cents - paid, 0) / 100, 2),
        "payments": [
            {
                "method": p.method,
                "amount": round(p.amount_cents / 100, 2),
                "received_at": p.received_at,
            }
            for p in order.payments
        ],
        # A receipt for money not yet received is a BILL, and saying so keeps a
        # pro-forma from being waved about as proof of payment.
        "is_paid": paid >= order.total_cents and order.total_cents > 0,
    }


@router.post("/api/orders/{order_id}/receipt")
async def issue_receipt(order_id: str, user: SubscribedUser, db: DbDep):
    """Get the shareable link for an order, creating it on first request.

    Open to every role: a waiter closing a table is exactly who a customer asks
    for a receipt.
    """
    result = await db.execute(
        select(Order).where(
            Order.id == order_id, Order.restaurant_id == user.restaurant_id
        )
    )
    order = result.scalar_one_or_none()
    if not order:
        raise APIError("Order not found", status=404)

    if not order.receipt_token:
        order.receipt_token = generate_link_token()
        await db.commit()
        await db.refresh(order)

    base = settings.PUBLIC_WEB_URL.rstrip("/")
    return ok(
        {
            "token": order.receipt_token,
            "url": f"{base}/receipt/{order.receipt_token}",
        }
    )


@router.get("/api/receipt/{token}")
@limiter.limit("60/minute")
async def public_receipt(token: str, request: Request, db: DbDep):
    """The receipt itself. No authentication — see the module docstring.

    Deliberately NOT behind require_subscription either: a receipt already
    given to a customer must not stop working because the restaurant's own
    subscription lapsed. Their record of a purchase is not the restaurant's
    billing status.
    """
    result = await db.execute(
        select(Order)
        .where(Order.receipt_token == token)
        .options(
            selectinload(Order.items),
            selectinload(Order.payments),
            selectinload(Order.server),
        )
    )
    order = result.scalar_one_or_none()
    if not order:
        raise APIError("Receipt not found", status=404)

    restaurant = await db.get(Restaurant, order.restaurant_id)
    return ok(_receipt_dict(order, restaurant))
