"""Billing routes: subscription status, owner-initiated pay/retry, charge
history, and the Paystack webhook receiver (public, verified, idempotent)."""
import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.core.config import settings
from app.core.dependencies import (
    CurrentUser,
    DbDep,
    get_current_restaurant,
    require_roles,
)
from app.core.security import APIError
from app.core.serializers import charge_dict, subscription_dict
from app.models import BillingCharge, ChargeStatus, Restaurant, Subscription, UserRole
from app.schemas.common import ok
from app.schemas.order import PayRequest
from app.services import billing, paystack

router = APIRouter(prefix="/api/billing", tags=["billing"])

MANAGERS = UserRole.MANAGERS


@router.get("/subscription")
async def get_subscription(user: CurrentUser, db: DbDep):
    result = await db.execute(
        select(Subscription).where(Subscription.restaurant_id == user.restaurant_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise APIError("No subscription found", status=404)
    return ok({"subscription": subscription_dict(sub)})


@router.post("/pay")
async def pay_now(
    body: PayRequest,
    db: DbDep,
    restaurant: Restaurant = Depends(get_current_restaurant),
    _manager=Depends(require_roles(*UserRole.MANAGERS)),
):
    """Owner-initiated charge (convert trial early / retry). Idempotent."""
    result = await db.execute(
        select(Subscription).where(Subscription.restaurant_id == restaurant.id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise APIError("No subscription found", status=404)

    phone = body.phone or restaurant.billing_phone
    if not phone:
        raise APIError("No billing phone on file. Add one to pay.", status=422, errors={"phone": "required"})
    if body.phone:
        restaurant.billing_phone = body.phone
        await db.commit()

    charge = await billing.initiate_charge(db, sub.id, phone_override=phone)

    refreshed = await db.execute(
        select(Subscription).where(Subscription.restaurant_id == restaurant.id)
    )
    sub = refreshed.scalar_one()
    return ok(
        {"charge": charge_dict(charge), "subscription": subscription_dict(sub)},
        message=(
            "Payment request sent — approve the M-Pesa prompt on your phone."
            if charge.status in ChargeStatus.OPEN
            else "Charge processed."
        ),
    )


@router.get("/charges")
async def list_charges(db: DbDep, user=Depends(require_roles(*MANAGERS))):
    result = await db.execute(
        select(Subscription).where(Subscription.restaurant_id == user.restaurant_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return ok({"charges": []})

    charges_res = await db.execute(
        select(BillingCharge)
        .where(BillingCharge.subscription_id == sub.id)
        .order_by(BillingCharge.requested_at.desc())
        .limit(50)
    )
    charges = charges_res.scalars().all()
    return ok({"charges": [charge_dict(c) for c in charges]})


# --- Paystack webhook (public, signature-verified) --------------------------
@router.post("/webhook/paystack")
async def paystack_webhook(request: Request, db: DbDep):
    """Receive a transaction result from Paystack.

    Authenticated by the x-paystack-signature header (HMAC-SHA512 of the body,
    keyed with our secret key), then processed idempotently. Always acks 200 on
    an authentic webhook so Paystack stops retrying — it re-sends every 3
    minutes, then hourly for 72 hours, until it sees one.
    """
    # The signature covers the bytes exactly as sent. Parsing to JSON and
    # re-serialising reorders keys and changes whitespace, producing a
    # different digest — so hash the raw body, then parse.
    raw_body = await request.body()
    if not paystack.verify_webhook(raw_body, request.headers.get("x-paystack-signature")):
        raise APIError("Not found", status=404)

    # Defence in depth behind the signature, and off by default: Paystack's
    # published source IPs can change, and a stale allowlist would 403 every
    # real webhook.
    if settings.paystack_allowed_ips:
        client_ip = request.client.host if request.client else None
        if client_ip not in settings.paystack_allowed_ips:
            raise APIError("Forbidden", status=403)

    try:
        event = json.loads(raw_body)
    except ValueError:
        event = {}

    try:
        await billing.process_webhook(db, event, raw_body.decode("utf-8", "replace")[:5000])
    except Exception:
        await db.rollback()

    return {"status": "ok"}
