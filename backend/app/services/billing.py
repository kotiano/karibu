"""Async billing engine.

Same four safeguards as the Flask version (see models/subscription.py docstring):
one open charge per period (DB unique index), row locking, idempotency keys, and
idempotent callback processing against terminal charges. The logic is identical;
only the DB calls are async.

All functions take an AsyncSession so they compose with FastAPI's request
session and with the scheduler's own session.
"""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    BillingCharge,
    ChargeStatus,
    ProcessedCallback,
    Restaurant,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
)
from app.services import email as email_service
from app.services import mpesa


async def _notify_owner_payment_issue(db: AsyncSession, sub: Subscription) -> None:
    """Email the restaurant owner about a failed payment or suspension.

    Called AFTER the billing transaction commits (state is final). Best-effort:
    never raises into the billing flow.
    """
    try:
        restaurant = await db.get(Restaurant, sub.restaurant_id)
        if not restaurant:
            return
        owner = (
            await db.execute(
                select(User)
                .where(
                    User.restaurant_id == sub.restaurant_id,
                    User.role == UserRole.OWNER,
                )
                .order_by(User.created_at)
            )
        ).scalars().first()
        if not owner:
            return

        pay_url = f"{settings.PUBLIC_API_URL}"  # app deep-link target / info page
        if sub.status == SubscriptionStatus.SUSPENDED:
            subject, html, text = email_service.subscription_suspended(
                owner.full_name, pay_url
            )
        else:
            when = (
                sub.next_retry_at.strftime("on %d %b at %H:%M UTC")
                if sub.next_retry_at
                else "soon"
            )
            subject, html, text = email_service.payment_failed(
                owner.full_name, when, pay_url
            )
        await email_service.send_email(owner.email, subject, html, text)
    except Exception:
        import logging

        logging.getLogger("karibu.billing").exception("owner notify failed")


# --- Locking helper --------------------------------------------------------
async def _lock_subscription(db: AsyncSession, subscription_id: str) -> Subscription | None:
    """Fetch a subscription with a row lock (serialises concurrent billing).

    with_for_update is enforced on Postgres; ignored on SQLite (dev), where the
    open-charge unique index still guarantees correctness.
    """
    stmt = select(Subscription).where(Subscription.id == subscription_id)
    if not settings.is_sqlite:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _open_charge_for(db: AsyncSession, subscription_id: str) -> BillingCharge | None:
    stmt = (
        select(BillingCharge)
        .where(
            BillingCharge.subscription_id == subscription_id,
            BillingCharge.status.in_(ChargeStatus.OPEN),
        )
        .order_by(BillingCharge.requested_at.desc())
    )
    result = await db.execute(stmt)
    return result.scalars().first()


# --- Subscription setup ----------------------------------------------------
async def start_trial(db: AsyncSession, restaurant: Restaurant, phone: str | None) -> Subscription:
    """Create a trialing subscription for a brand-new restaurant."""
    now = datetime.utcnow()
    sub = Subscription(
        restaurant_id=restaurant.id,
        status=SubscriptionStatus.TRIALING,
        price_cents=settings.SUBSCRIPTION_PRICE_CENTS,
        currency=settings.SUBSCRIPTION_CURRENCY,
        trial_ends_at=now + timedelta(days=settings.TRIAL_DAYS),
    )
    db.add(sub)
    if phone:
        restaurant.billing_phone = phone
    await db.flush()
    return sub


# --- Charge creation -------------------------------------------------------
def _period_for(sub: Subscription, now: datetime) -> tuple[datetime, datetime]:
    days = settings.BILLING_PERIOD_DAYS
    start = sub.current_period_end or now
    if start < now - timedelta(days=days):
        start = now
    return start, start + timedelta(days=days)


def _idempotency_key(sub: Subscription, period_start: datetime, attempt: int) -> str:
    return f"sub_{sub.id}_{period_start.strftime('%Y%m%d')}_a{attempt}"


def _callback_url() -> str:
    base = settings.MPESA_CALLBACK_URL.rstrip("/")
    secret = settings.MPESA_CALLBACK_SECRET
    return f"{base}/{secret}" if base else f"https://example.invalid/{secret}"


async def initiate_charge(
    db: AsyncSession,
    subscription_id: str,
    *,
    attempt: int | None = None,
    phone_override: str | None = None,
) -> BillingCharge:
    """Create (or return the existing) open charge and push STK. Idempotent."""
    sub = await _lock_subscription(db, subscription_id)
    if not sub:
        raise ValueError("Subscription not found")

    now = datetime.utcnow()

    # 1) Reuse any open charge — never stack a second STK on one awaiting a callback.
    existing_open = await _open_charge_for(db, sub.id)
    if existing_open:
        await db.commit()
        return existing_open

    period_start, period_end = _period_for(sub, now)
    attempt = attempt or (sub.failed_attempts + 1)
    key = _idempotency_key(sub, period_start, attempt)

    # 2) Idempotency by key.
    dup_res = await db.execute(
        select(BillingCharge).where(BillingCharge.idempotency_key == key)
    )
    dup = dup_res.scalar_one_or_none()
    if dup:
        await db.commit()
        return dup

    # Load the restaurant for the billing phone.
    restaurant = await db.get(Restaurant, sub.restaurant_id)

    charge = BillingCharge(
        subscription_id=sub.id,
        status=ChargeStatus.PENDING,
        amount_cents=sub.price_cents,
        currency=sub.currency,
        period_start=period_start,
        period_end=period_end,
        idempotency_key=key,
        attempt_number=attempt,
        phone=phone_override or (restaurant.billing_phone if restaurant else None),
    )
    db.add(charge)

    try:
        # 3) DB-level stop: partial unique index rejects a concurrent 2nd open charge.
        await db.flush()
    except IntegrityError:
        await db.rollback()
        # Someone else created it — return theirs.
        open_charge = await _open_charge_for(db, subscription_id)
        await db.commit()
        if open_charge:
            return open_charge
        raise

    # Push STK (or simulate). On failure, fail the charge cleanly so a retry can
    # create a fresh one.
    try:
        result = await mpesa.stk_push(
            phone=charge.phone or "",
            amount_cents=charge.amount_cents,
            account_reference="KaribuPOS",
            description="Subscription",
            callback_url=_callback_url(),
        )
    except mpesa.MpesaError as exc:
        charge.mark_failed(None, f"STK push failed: {exc}")
        await db.commit()
        return charge

    charge.mpesa_checkout_id = result["checkout_id"]
    charge.mpesa_merchant_id = result.get("merchant_id")
    charge.status = ChargeStatus.PROCESSING
    await db.commit()
    return charge


# --- Callback processing ---------------------------------------------------
async def process_callback(db: AsyncSession, payload: dict, raw_body: str) -> str:
    """Apply an STK callback exactly once. Returns a short status string."""
    stk = (payload.get("Body") or {}).get("stkCallback") or {}
    checkout_id = stk.get("CheckoutRequestID")
    if not checkout_id:
        return "ignored_no_checkout_id"

    result_code = stk.get("ResultCode")
    result_desc = stk.get("ResultDesc", "")

    # Dedupe at the ledger level first (before locking anything).
    dup_res = await db.execute(
        select(ProcessedCallback).where(ProcessedCallback.checkout_id == checkout_id)
    )
    if dup_res.scalar_one_or_none():
        return "duplicate_ignored"

    charge_res = await db.execute(
        select(BillingCharge).where(BillingCharge.mpesa_checkout_id == checkout_id)
    )
    charge = charge_res.scalar_one_or_none()
    if not charge:
        db.add(
            ProcessedCallback(
                checkout_id=checkout_id, result_code=result_code, raw_payload=raw_body
            )
        )
        await db.commit()
        return "no_matching_charge"

    # Lock the parent subscription for the transition.
    sub = await _lock_subscription(db, charge.subscription_id)
    charge = await db.get(BillingCharge, charge.id)

    if charge.is_terminal:
        db.add(
            ProcessedCallback(
                checkout_id=checkout_id, result_code=result_code, raw_payload=raw_body
            )
        )
        await db.commit()
        return "already_finalized"

    if result_code == 0:
        receipt = _extract_receipt(stk)
        charge.mark_success(receipt, result_code, result_desc)
        await _apply_successful_payment(db, sub, charge)
        outcome = "success"
    else:
        charge.mark_failed(result_code, result_desc)
        await _apply_failed_payment(db, sub, charge)
        outcome = "failed"

    db.add(
        ProcessedCallback(
            checkout_id=checkout_id, result_code=result_code, raw_payload=raw_body
        )
    )
    await db.commit()

    # After commit, notify the owner if this was a failure (dunning/suspension).
    if outcome == "failed":
        await _notify_owner_payment_issue(db, sub)
    return outcome


def _extract_receipt(stk: dict) -> str | None:
    items = (stk.get("CallbackMetadata") or {}).get("Item") or []
    for item in items:
        if item.get("Name") == "MpesaReceiptNumber":
            return str(item.get("Value"))
    return None


async def _apply_successful_payment(db: AsyncSession, sub: Subscription, charge: BillingCharge):
    sub.status = SubscriptionStatus.ACTIVE
    sub.current_period_start = charge.period_start
    sub.current_period_end = charge.period_end
    sub.failed_attempts = 0
    sub.next_retry_at = None
    restaurant = await db.get(Restaurant, sub.restaurant_id)
    if restaurant:
        restaurant.is_active = True


async def _apply_failed_payment(db: AsyncSession, sub: Subscription, charge: BillingCharge):
    sub.failed_attempts += 1
    retry_schedule = settings.dunning_retry_hours

    if sub.failed_attempts >= len(retry_schedule):
        sub.status = SubscriptionStatus.SUSPENDED
        sub.next_retry_at = None
        # Load the restaurant explicitly — async forbids implicit lazy loading.
        restaurant = await db.get(Restaurant, sub.restaurant_id)
        if restaurant:
            restaurant.is_active = False
    else:
        sub.status = SubscriptionStatus.PAST_DUE
        hours = retry_schedule[sub.failed_attempts]
        sub.next_retry_at = datetime.utcnow() + timedelta(hours=hours)


# --- Dunning / renewal sweep ----------------------------------------------
async def run_billing_sweep(db: AsyncSession, now: datetime | None = None) -> dict:
    """Idempotent periodic job: charge ended trials, due renewals, and past-due
    retries; fail stale charges. Safe to run repeatedly."""
    now = now or datetime.utcnow()
    stats = {"charged": 0, "stale_failed": 0, "skipped": 0}

    await _expire_stale_charges(db, now, stats)

    result = await db.execute(
        select(Subscription).where(
            Subscription.status.in_(
                (
                    SubscriptionStatus.TRIALING,
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.PAST_DUE,
                )
            )
        )
    )
    candidates = result.scalars().all()

    for sub in candidates:
        if sub.status == SubscriptionStatus.PAST_DUE:
            due = sub.next_retry_at is not None and now >= sub.next_retry_at
        else:
            due = sub.is_renewal_due(now)

        if not due:
            stats["skipped"] += 1
            continue

        try:
            charge = await initiate_charge(db, sub.id)
            if charge.status in ChargeStatus.OPEN or charge.status == ChargeStatus.SUCCESS:
                stats["charged"] += 1
        except Exception:
            # Never let one tenant break the sweep.
            await db.rollback()

    return stats


async def _expire_stale_charges(db: AsyncSession, now: datetime, stats: dict):
    """Fail PROCESSING charges with no callback so they can be retried."""
    cutoff = now - timedelta(minutes=settings.CHARGE_STALE_MINUTES)
    result = await db.execute(
        select(BillingCharge).where(
            BillingCharge.status == ChargeStatus.PROCESSING,
            BillingCharge.requested_at < cutoff,
        )
    )
    stale = result.scalars().all()
    for charge in stale:
        sub = await _lock_subscription(db, charge.subscription_id)
        fresh = await db.get(BillingCharge, charge.id)
        if fresh.is_terminal:
            continue
        fresh.mark_failed(None, "No callback received (timed out)")
        await _apply_failed_payment(db, sub, fresh)
        stats["stale_failed"] += 1
    await db.commit()
