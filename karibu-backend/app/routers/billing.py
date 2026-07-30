"""Billing routes: subscription status, owner-initiated pay/retry, charge
history, Google Play purchase verification, and the gateway webhook receivers
(public, verified, idempotent)."""
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
from app.schemas.order import PayRequest, PlayPurchaseIn
from app.services import billing, google_play, paystack

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


# --- Google Play Billing ----------------------------------------------------
@router.post("/google/verify")
async def verify_play_purchase(
    body: PlayPurchaseIn,
    db: DbDep,
    restaurant: Restaurant = Depends(get_current_restaurant),
    _owner=Depends(require_roles(UserRole.OWNER)),
):
    """Redeem a Google Play purchase token for entitlement.

    The app calls this after Play reports a successful purchase. The token is
    the *only* thing accepted from the client — no price, no product state, no
    "isActive" flag — because everything else is re-fetched from Google. A
    client claim is not evidence of payment.
    """
    if not settings.play_billing_enabled:
        raise APIError("Google Play billing is not configured", status=503)

    try:
        sub = await billing.verify_and_link_purchase(
            db, restaurant.id, body.purchase_token.strip()
        )
    except google_play.PurchaseNotFound:
        raise APIError(
            "Google has no record of this purchase. If you were charged, "
            "reopen the app in a few minutes and it will be applied.",
            status=404,
        )
    except ValueError as exc:
        raise APIError(str(exc), status=409)
    except google_play.GooglePlayError as exc:
        # Google unreachable or misconfigured. 502 so the client retries rather
        # than treating it as a rejected purchase.
        raise APIError(f"Could not verify with Google Play: {exc}", status=502)

    return ok(
        {"subscription": subscription_dict(sub)},
        message="Subscription active — asante!",
    )


@router.post("/webhook/google/{secret}")
async def google_play_webhook(secret: str, request: Request, db: DbDep):
    """Receive a Real-time Developer Notification from Google Play.

    Pub/Sub push cannot send custom headers or sign its payload, so the only
    edge authentication available is a secret path segment — compared in
    constant time. That is thin on its own, which is why the notification is
    treated purely as a nudge: the purchase token inside it is re-verified
    against Google's API before anything changes, so a forged notification
    grants no entitlement.

    Always 200s on an authentic request. Pub/Sub redelivers anything else, and
    a retry storm on a genuine bug is worse than a dropped notification we will
    reconcile on the next sweep anyway.
    """
    expected = settings.GOOGLE_PLAY_RTDN_SECRET
    if not expected or not hmac.compare_digest(secret, expected):
        raise APIError("Not found", status=404)

    try:
        envelope = await request.json()
    except Exception:
        envelope = {}

    note = google_play.decode_rtdn(envelope)
    if not note or note.get("test"):
        # Google's one-off wiring test, or a notification type we don't act on.
        return {"status": "ok"}

    try:
        outcome = await billing.process_play_notification(db, note)
    except Exception:
        await db.rollback()
        outcome = "error"
    return {"status": "ok", "outcome": outcome}


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
