"""Billing routes: subscription status, owner-initiated pay/retry, charge
history, and the M-Pesa STK callback receiver (public, verified, idempotent)."""
import hmac
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
from app.services import billing

router = APIRouter(prefix="/api/billing", tags=["billing"])

MANAGERS = (UserRole.OWNER, UserRole.MANAGER)


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
    _owner=Depends(require_roles(UserRole.OWNER)),
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


# --- M-Pesa callback (public, verified) ------------------------------------
@router.post("/callback/{secret}")
async def mpesa_callback(secret: str, request: Request, db: DbDep):
    """Receive an STK result from Safaricom.

    Verified by a secret path segment + optional IP allowlist, then processed
    idempotently. Always acks 200 so Safaricom doesn't retry endlessly.
    """
    # Constant-time comparison: a plain != leaks how many leading characters
    # matched via response timing, letting an attacker recover the secret
    # byte-by-byte. compare_digest closes that channel.
    if not secret or not hmac.compare_digest(secret, settings.MPESA_CALLBACK_SECRET):
        raise APIError("Not found", status=404)

    if settings.callback_allowed_ips:
        client_ip = request.client.host if request.client else None
        if client_ip not in settings.callback_allowed_ips:
            raise APIError("Forbidden", status=403)

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    raw = json.dumps(payload)[:5000]

    try:
        await billing.process_callback(db, payload, raw)
    except Exception:
        await db.rollback()

    return {"ResultCode": 0, "ResultDesc": "Accepted"}
