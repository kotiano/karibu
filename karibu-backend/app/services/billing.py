"""Async billing engine.

Same four safeguards as ever (see models/subscription.py docstring): one open
charge per period (DB unique index), row locking, idempotency keys, and
idempotent webhook processing against terminal charges. Swapping the payment
gateway from Daraja to Paystack changed only how a charge is initiated and
confirmed — every double-billing guarantee below is enforced by the database,
not the gateway, so none of it moved.

All functions take an AsyncSession so they compose with FastAPI's request
session and with the scheduler's own session.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    BillingCharge,
    BillingProvider,
    ChargeStatus,
    ProcessedCallback,
    Restaurant,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
)
from app.services import email as email_service
from app.services import google_play, paystack


async def _owner_for(db: AsyncSession, restaurant_id: str) -> User | None:
    """The restaurant's owner — the human a charge is billed to.

    Paystack keys every transaction to a customer email (unlike Daraja, which
    only ever saw a phone number), so this is now on the charge path and not
    just the notification path.
    """
    result = await db.execute(
        select(User)
        .where(User.restaurant_id == restaurant_id, User.role == UserRole.OWNER)
        .order_by(User.created_at)
    )
    return result.scalars().first()


async def _notify_owner_payment_issue(db: AsyncSession, sub: Subscription) -> None:
    """Email the restaurant owner about a failed payment or suspension.

    Called AFTER the billing transaction commits (state is final). Best-effort:
    never raises into the billing flow.
    """
    try:
        restaurant = await db.get(Restaurant, sub.restaurant_id)
        if not restaurant:
            return
        owner = await _owner_for(db, sub.restaurant_id)
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

    # Load the restaurant for the billing phone, and the owner for the email
    # Paystack requires on every transaction.
    restaurant = await db.get(Restaurant, sub.restaurant_id)
    owner = await _owner_for(db, sub.restaurant_id)

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

    # Paystack bills a customer email, not just a phone. Without an owner on
    # file there is nobody to charge — fail the charge rather than send a
    # malformed request the gateway would reject anyway.
    if not owner:
        charge.mark_failed(None, "No owner account on file to bill")
        await db.commit()
        return charge

    # Push the M-Pesa STK prompt (or simulate). On failure, fail the charge
    # cleanly so a retry can create a fresh one.
    try:
        result = await paystack.charge_mobile_money(
            email=owner.email,
            phone=charge.phone or "",
            amount_cents=charge.amount_cents,
            currency=charge.currency,
            metadata={
                "restaurant_id": sub.restaurant_id,
                "subscription_id": sub.id,
                "charge_id": charge.id,
                "idempotency_key": key,
            },
        )
    except paystack.PaystackError as exc:
        # result_desc is String(255); an unbounded gateway message would raise
        # a DataError on Postgres and lose the charge's failure state entirely.
        charge.mark_failed(None, f"Payment request failed: {exc}"[:255])
        await db.commit()
        return charge

    charge.provider_reference = result["reference"]

    # Only "success"/"failed" are final. Anything else (normally "pay_offline"
    # — the customer is being asked for their PIN) stays PROCESSING until the
    # webhook lands, with _expire_stale_charges as the backstop if it never
    # does. Treating an unrecognised state as failure here would be the unsafe
    # choice: the customer can still complete the payment seconds later.
    if result["status"] == paystack.STATUS_SUCCESS:
        charge.mark_success(result["reference"], 0, "Paid")
        await _apply_successful_payment(db, sub, charge)
    elif result["status"] == paystack.STATUS_FAILED:
        charge.mark_failed(
            None, (result.get("display_text") or "Payment declined")[:255]
        )
        await _apply_failed_payment(db, sub, charge)
    else:
        charge.status = ChargeStatus.PROCESSING

    await db.commit()
    return charge


# --- Google Play Billing ----------------------------------------------------
async def _apply_play_state(
    db: AsyncSession, sub: Subscription, state: dict
) -> None:
    """Mirror Google's view of a subscription onto ours.

    Google owns the lifecycle here, so this only ever copies — it never decides.
    Dunning counters are cleared because retry scheduling is Google's job on
    this rail; leaving stale values would make the Paystack sweep think the
    subscription needed chasing.
    """
    sub.provider = BillingProvider.GOOGLE_PLAY
    sub.google_expiry_at = state["expiry"]
    sub.failed_attempts = 0
    sub.next_retry_at = None

    if state["entitled"]:
        sub.status = SubscriptionStatus.ACTIVE
        sub.current_period_end = state["expiry"]
        if not sub.current_period_start:
            sub.current_period_start = datetime.utcnow()
    else:
        # Expired, revoked, on hold or paused — access stops. Google has already
        # given the user every chance to fix payment by this point.
        sub.status = SubscriptionStatus.SUSPENDED

    restaurant = await db.get(Restaurant, sub.restaurant_id)
    if restaurant:
        restaurant.is_active = state["entitled"]


async def verify_and_link_purchase(
    db: AsyncSession, restaurant_id: str, purchase_token: str
) -> Subscription:
    """Verify a Play purchase with Google and attach it to this restaurant.

    The client sends a purchase token; a client claim proves nothing, so the
    token is checked against Google before any entitlement is granted.

    Raises GooglePlayError / PurchaseNotFound on a token Google won't confirm,
    and ValueError if the token already belongs to a different restaurant.
    """
    state = await google_play.get_subscription(purchase_token)

    # A purchase may supersede an earlier one (upgrade, resubscribe). Follow the
    # chain so the old row is reused rather than stranding a second subscription
    # pointing at a dead token.
    tokens = [purchase_token]
    if state.get("linked_token"):
        tokens.append(state["linked_token"])

    existing = (
        await db.execute(
            select(Subscription).where(Subscription.google_purchase_token.in_(tokens))
        )
    ).scalars().first()

    if existing and existing.restaurant_id != restaurant_id:
        # Someone is replaying another tenant's receipt. The unique index would
        # stop it anyway; failing here gives a clear reason instead of a 500.
        raise ValueError("This purchase is already linked to another restaurant")

    sub = existing or (
        await db.execute(
            select(Subscription).where(Subscription.restaurant_id == restaurant_id)
        )
    ).scalar_one_or_none()

    if not sub:
        raise ValueError("No subscription found for this restaurant")

    sub.google_purchase_token = purchase_token
    await _apply_play_state(db, sub, state)
    await db.commit()

    # Acknowledge AFTER committing entitlement. Google revokes and refunds
    # anything unacknowledged within three days, but acknowledging before the
    # grant is durable would risk telling Google we delivered something we then
    # failed to record.
    if state["entitled"] and not state["acknowledged"]:
        try:
            await google_play.acknowledge(purchase_token)
        except google_play.GooglePlayError:
            # The reconcile sweep retries this; a failure here must not undo a
            # purchase the customer has already paid for.
            logging.getLogger("karibu.billing").exception(
                "Failed to acknowledge Play purchase — will retry on next sweep"
            )
    return sub


async def process_play_notification(db: AsyncSession, note: dict) -> str:
    """Apply a Real-time Developer Notification.

    The notification itself is only a nudge — it carries a type but we never
    trust it. The purchase token is re-verified against Google's API, so a
    forged notification hitting the endpoint grants nothing.
    """
    token = note.get("purchase_token")
    if not token:
        return "ignored_no_token"

    sub = (
        await db.execute(
            select(Subscription).where(Subscription.google_purchase_token == token)
        )
    ).scalar_one_or_none()
    if not sub:
        # Common and harmless: the RTDN can beat the client's verify call.
        # verify_and_link_purchase will fetch fresh state moments later.
        return "no_matching_subscription"

    try:
        state = await google_play.get_subscription(token)
    except google_play.PurchaseNotFound:
        sub.status = SubscriptionStatus.SUSPENDED
        restaurant = await db.get(Restaurant, sub.restaurant_id)
        if restaurant:
            restaurant.is_active = False
        await db.commit()
        return "purchase_gone"

    await _apply_play_state(db, sub, state)
    await db.commit()

    if not state["entitled"]:
        await _notify_owner_payment_issue(db, sub)
    return f"applied_{state['state']}"


# --- Webhook processing ----------------------------------------------------
# Paystack fires charge.success on payment and charge.failed when the customer
# cancels or has insufficient funds. Anything else (transfers, refunds,
# subscriptions we don't use) is acknowledged and ignored.
_SUCCESS_EVENTS = {"charge.success"}
_FAILURE_EVENTS = {"charge.failed"}


async def process_webhook(db: AsyncSession, event: dict, raw_body: str) -> str:
    """Apply a Paystack webhook exactly once. Returns a short status string.

    The caller must have already verified the HMAC signature — this function
    trusts its input.
    """
    event_type = (event.get("event") or "").lower()
    data = event.get("data") or {}
    reference = data.get("reference")
    if not reference:
        return "ignored_no_reference"
    if event_type not in _SUCCESS_EVENTS and event_type not in _FAILURE_EVENTS:
        return f"ignored_event_{event_type or 'unknown'}"

    succeeded = event_type in _SUCCESS_EVENTS
    # Paystack's own wording for why a charge ended how it did.
    result_desc = (data.get("gateway_response") or event_type)[:255]

    # Dedupe at the ledger level first (before locking anything). Paystack
    # retries until it sees a 200, so this path is hit routinely.
    dup_res = await db.execute(
        select(ProcessedCallback).where(ProcessedCallback.reference == reference)
    )
    if dup_res.scalar_one_or_none():
        return "duplicate_ignored"

    charge_res = await db.execute(
        select(BillingCharge).where(BillingCharge.provider_reference == reference)
    )
    charge = charge_res.scalar_one_or_none()
    if not charge:
        db.add(
            ProcessedCallback(
                reference=reference,
                result_code=0 if succeeded else 1,
                raw_payload=raw_body,
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
                reference=reference,
                result_code=0 if succeeded else 1,
                raw_payload=raw_body,
            )
        )
        await db.commit()
        return "already_finalized"

    if succeeded:
        charge.mark_success(paystack.extract_receipt(data), 0, result_desc)
        await _apply_successful_payment(db, sub, charge)
        outcome = "success"
    else:
        charge.mark_failed(1, result_desc)
        await _apply_failed_payment(db, sub, charge)
        outcome = "failed"

    db.add(
        ProcessedCallback(
            reference=reference,
            result_code=0 if succeeded else 1,
            raw_payload=raw_body,
        )
    )
    await db.commit()

    # After commit, notify the owner if this was a failure (dunning/suspension).
    if outcome == "failed":
        await _notify_owner_payment_issue(db, sub)
    return outcome


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
    stats = {"charged": 0, "stale_failed": 0, "reconciled": 0, "skipped": 0}

    await _expire_stale_charges(db, now, stats)

    result = await db.execute(
        select(Subscription).where(
            Subscription.status.in_(
                (
                    SubscriptionStatus.TRIALING,
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.PAST_DUE,
                )
            ),
            # Google owns renewal for Play-billed subscriptions. Charging one
            # here would bill the same restaurant twice — once through Google's
            # auto-renewal and again through Paystack — so they are excluded at
            # the query, not by a check further down that a later edit could
            # skip past.
            Subscription.provider == BillingProvider.PAYSTACK,
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
    """Fail PROCESSING charges with no webhook so they can be retried.

    Before failing one, ask Paystack what actually happened. A webhook can be
    lost — a deploy mid-flight, a cold start that times out the delivery — and
    failing a charge the customer really paid is the worst outcome available
    here: it drives the subscription toward suspension and the dunning sweep
    bills them a second time. One verify call per stale charge removes that.
    """
    cutoff = now - timedelta(minutes=settings.CHARGE_STALE_MINUTES)
    result = await db.execute(
        select(BillingCharge).where(
            BillingCharge.status == ChargeStatus.PROCESSING,
            BillingCharge.requested_at < cutoff,
        )
    )
    stale = result.scalars().all()
    for charge in stale:
        settled = None
        if charge.provider_reference and paystack.is_configured():
            try:
                data = await paystack.verify_transaction(charge.provider_reference)
                settled = (data.get("status") or "").lower()
            except paystack.PaystackError:
                # Gateway unreachable — leave the charge alone and re-check on
                # the next sweep rather than guessing.
                continue

        sub = await _lock_subscription(db, charge.subscription_id)
        fresh = await db.get(BillingCharge, charge.id)
        if fresh.is_terminal:
            continue

        if settled == paystack.STATUS_SUCCESS:
            fresh.mark_success(paystack.extract_receipt(data), 0, "Reconciled")
            await _apply_successful_payment(db, sub, fresh)
            stats["reconciled"] += 1
            continue
        if settled and settled not in (paystack.STATUS_FAILED, "abandoned", "reversed"):
            # Still genuinely pending at the gateway — give it another cycle.
            continue

        fresh.mark_failed(None, "No confirmation received (timed out)")
        await _apply_failed_payment(db, sub, fresh)
        stats["stale_failed"] += 1
    await db.commit()
