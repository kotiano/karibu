"""Platform-admin monitoring surface (/api/admin/*).

This is the SaaS operator's view ACROSS all tenants — the one place tenant
isolation is deliberately bypassed. Because of that it is:

- gated by `require_platform_admin` (flag re-checked in the DB every request,
  404 to everyone else so the surface can't even be probed),
- strictly READ-ONLY (GET only) — monitoring, not remote control,
- paginated and capped everywhere, aggregated in SQL, same performance
  discipline as the tenant routes.

The admin flag itself is only settable via create_admin.py, never any API.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from app.core.audit import record_audit
from app.core.dependencies import DbDep, PlatformAdmin
from app.core.security import APIError
from app.core.serializers import charge_dict, subscription_dict
from app.models import (
    AuditAction,
    AuditLog,
    BillingCharge,
    ChargeStatus,
    Order,
    Payment,
    Restaurant,
    Subscription,
    SubscriptionStatus,
    User,
)
from app.schemas.common import ok

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Overview: the "how is the whole platform doing" numbers.
# ---------------------------------------------------------------------------
@router.get("/overview")
async def overview(admin: PlatformAdmin, db: DbDep):
    now = datetime.utcnow()
    today = _day_start(now)
    month_ago = now - timedelta(days=30)

    # Restaurants by subscription status (one GROUP BY).
    status_res = await db.execute(
        select(Subscription.status, func.count()).group_by(Subscription.status)
    )
    by_status = {status: count for status, count in status_res.all()}

    # Counted through the subscription for the same reason as the list above:
    # the platform's own HQ row is not a customer, and reporting it as one
    # overstates the business to the person least able to notice.
    restaurants_total = (
        await db.execute(
            select(func.count()).select_from(Subscription)
        )
    ).scalar() or 0
    users_total = (
        await db.execute(select(func.count()).select_from(User))
    ).scalar() or 0

    # Subscription revenue (money YOU receive): successful charges.
    async def sub_revenue_since(since: datetime | None) -> int:
        stmt = select(func.coalesce(func.sum(BillingCharge.amount_cents), 0)).where(
            BillingCharge.status == ChargeStatus.SUCCESS
        )
        if since is not None:
            stmt = stmt.where(BillingCharge.finalized_at >= since)
        return (await db.execute(stmt)).scalar() or 0

    sub_revenue_total = await sub_revenue_since(None)
    sub_revenue_30d = await sub_revenue_since(month_ago)

    failed_charges_7d = (
        await db.execute(
            select(func.count())
            .select_from(BillingCharge)
            .where(
                BillingCharge.status == ChargeStatus.FAILED,
                BillingCharge.requested_at >= now - timedelta(days=7),
            )
        )
    ).scalar() or 0

    # Restaurant activity today (their money, your platform's pulse).
    orders_today = (
        await db.execute(
            select(func.count()).select_from(Order).where(Order.created_at >= today)
        )
    ).scalar() or 0
    order_payments_today = (
        await db.execute(
            select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
                Payment.received_at >= today
            )
        )
    ).scalar() or 0

    return ok(
        {
            "restaurants_total": restaurants_total,
            "restaurants_by_status": by_status,
            "users_total": users_total,
            "subscription_revenue_total": round(sub_revenue_total / 100, 2),
            "subscription_revenue_30d": round(sub_revenue_30d / 100, 2),
            "failed_charges_7d": failed_charges_7d,
            "orders_today_all_restaurants": orders_today,
            "order_payments_today_all_restaurants": round(order_payments_today / 100, 2),
        }
    )


# ---------------------------------------------------------------------------
# Restaurants: every tenant, with subscription state.
# ---------------------------------------------------------------------------
@router.get("/restaurants")
async def list_restaurants(
    admin: PlatformAdmin,
    db: DbDep,
    status: str | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    # INNER join, not outer. A restaurant with no subscription is not a
    # customer — it is the "Karibu Platform HQ" placeholder create_admin.py
    # makes because every user needs a tenant. Listing it counted our own admin
    # row as a paying restaurant and offered subscription actions that could
    # only ever 404.
    stmt = (
        select(Restaurant, Subscription)
        .join(Subscription, Subscription.restaurant_id == Restaurant.id)
        .order_by(Restaurant.created_at.desc())
    )
    if status:
        stmt = stmt.where(Subscription.status == status)
    if search:
        stmt = stmt.where(Restaurant.name.ilike(f"%{search}%"))
    stmt = stmt.limit(limit).offset(offset)

    rows = (await db.execute(stmt)).all()
    data = [
        {
            "id": r.id,
            "name": r.name,
            "billing_phone": r.billing_phone,
            "is_active": r.is_active,
            "created_at": r.created_at,
            "subscription": subscription_dict(s) if s else None,
        }
        for r, s in rows
    ]
    return ok({"restaurants": data, "limit": limit, "offset": offset})


@router.get("/restaurants/{restaurant_id}")
async def restaurant_detail(restaurant_id: str, admin: PlatformAdmin, db: DbDep):
    restaurant = await db.get(Restaurant, restaurant_id)
    if not restaurant:
        raise APIError("Restaurant not found", status=404)

    sub = (
        await db.execute(
            select(Subscription).where(Subscription.restaurant_id == restaurant_id)
        )
    ).scalar_one_or_none()

    users = (
        (
            await db.execute(
                select(User)
                .where(User.restaurant_id == restaurant_id)
                .order_by(User.created_at)
            )
        )
        .scalars()
        .all()
    )

    charges = (
        (
            await db.execute(
                select(BillingCharge)
                .where(BillingCharge.subscription_id == (sub.id if sub else ""))
                .order_by(BillingCharge.requested_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )

    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    order_count_30d = (
        await db.execute(
            select(func.count())
            .select_from(Order)
            .where(
                Order.restaurant_id == restaurant_id,
                Order.created_at >= thirty_days_ago,
            )
        )
    ).scalar() or 0
    revenue_30d = (
        await db.execute(
            select(func.coalesce(func.sum(Payment.amount_cents), 0))
            .join(Order, Payment.order_id == Order.id)
            .where(
                Order.restaurant_id == restaurant_id,
                Payment.received_at >= thirty_days_ago,
            )
        )
    ).scalar() or 0

    return ok(
        {
            "restaurant": {
                "id": restaurant.id,
                "name": restaurant.name,
                "billing_phone": restaurant.billing_phone,
                "is_active": restaurant.is_active,
                "created_at": restaurant.created_at,
            },
            "subscription": subscription_dict(sub) if sub else None,
            "users": [
                {
                    "id": u.id,
                    "full_name": u.full_name,
                    "email": u.email,
                    "role": u.role,
                    "is_active": u.is_active,
                    "created_at": u.created_at,
                }
                for u in users
            ],
            "charges": [charge_dict(c) for c in charges],
            "orders_30d": order_count_30d,
            "order_revenue_30d": round(revenue_30d / 100, 2),
        }
    )


# ---------------------------------------------------------------------------
# Payments — both kinds, clearly separated:
#   /charges  = subscription payments (the money the PLATFORM receives)
#   /payments = order payments inside restaurants (the tenants' takings)
# ---------------------------------------------------------------------------
@router.get("/charges")
async def list_charges(
    admin: PlatformAdmin,
    db: DbDep,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = (
        select(BillingCharge, Restaurant.name)
        .join(Subscription, BillingCharge.subscription_id == Subscription.id)
        .join(Restaurant, Subscription.restaurant_id == Restaurant.id)
        .order_by(BillingCharge.requested_at.desc())
    )
    if status:
        stmt = stmt.where(BillingCharge.status == status)
    stmt = stmt.limit(limit).offset(offset)

    rows = (await db.execute(stmt)).all()
    data = [
        {**charge_dict(c), "restaurant_name": name} for c, name in rows
    ]
    return ok({"charges": data, "limit": limit, "offset": offset})


@router.get("/payments")
async def list_order_payments(
    admin: PlatformAdmin,
    db: DbDep,
    method: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = (
        select(Payment, Order.reference, Restaurant.name)
        .join(Order, Payment.order_id == Order.id)
        .join(Restaurant, Order.restaurant_id == Restaurant.id)
        .order_by(Payment.received_at.desc())
    )
    if method:
        stmt = stmt.where(Payment.method == method)
    stmt = stmt.limit(limit).offset(offset)

    rows = (await db.execute(stmt)).all()
    data = [
        {
            "id": p.id,
            "method": p.method,
            "amount": round(p.amount_cents / 100, 2),
            "reference": p.reference,
            "received_at": p.received_at,
            "order_reference": order_ref,
            "restaurant_name": rest_name,
        }
        for p, order_ref, rest_name in rows
    ]
    return ok({"payments": data, "limit": limit, "offset": offset})


# ---------------------------------------------------------------------------
# Users: everyone on the platform.
# ---------------------------------------------------------------------------
@router.get("/users")
async def list_users(
    admin: PlatformAdmin,
    db: DbDep,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = (
        select(User, Restaurant.name)
        .join(Restaurant, User.restaurant_id == Restaurant.id)
        .order_by(User.created_at.desc())
    )
    if search:
        like = f"%{search}%"
        stmt = stmt.where(User.email.ilike(like) | User.full_name.ilike(like))
    stmt = stmt.limit(limit).offset(offset)

    rows = (await db.execute(stmt)).all()
    data = [
        {
            "id": u.id,
            "full_name": u.full_name,
            "email": u.email,
            "role": u.role,
            "is_active": u.is_active,
            "is_platform_admin": u.is_platform_admin,
            "restaurant_name": rest_name,
            "created_at": u.created_at,
        }
        for u, rest_name in rows
    ]
    return ok({"users": data, "limit": limit, "offset": offset})


# ---------------------------------------------------------------------------
# Activity: "everything that happens", derived from the records themselves —
# signups, subscription charges, and orders merged into one recent timeline.
# ---------------------------------------------------------------------------
@router.get("/activity")
async def activity(
    admin: PlatformAdmin,
    db: DbDep,
    limit: int = Query(default=30, ge=1, le=100),
):
    per_source = limit  # over-fetch each source, merge, trim

    users_rows = (
        await db.execute(
            select(User.full_name, User.email, Restaurant.name, User.created_at)
            .join(Restaurant, User.restaurant_id == Restaurant.id)
            .order_by(User.created_at.desc())
            .limit(per_source)
        )
    ).all()
    charge_rows = (
        await db.execute(
            select(
                BillingCharge.status,
                BillingCharge.amount_cents,
                Restaurant.name,
                BillingCharge.requested_at,
            )
            .join(Subscription, BillingCharge.subscription_id == Subscription.id)
            .join(Restaurant, Subscription.restaurant_id == Restaurant.id)
            .order_by(BillingCharge.requested_at.desc())
            .limit(per_source)
        )
    ).all()
    order_rows = (
        await db.execute(
            select(Order.reference, Order.total_cents, Restaurant.name, Order.created_at)
            .join(Restaurant, Order.restaurant_id == Restaurant.id)
            .order_by(Order.created_at.desc())
            .limit(per_source)
        )
    ).all()

    events: list[dict] = []
    for full_name, email, rest, at in users_rows:
        events.append(
            {
                "type": "user_joined",
                "at": at,
                "title": f"{full_name} joined",
                "subtitle": f"{email} · {rest}",
            }
        )
    for status, cents, rest, at in charge_rows:
        events.append(
            {
                "type": f"charge_{status}",
                "at": at,
                "title": f"Subscription charge {status} · KSh {cents / 100:,.0f}",
                "subtitle": rest,
            }
        )
    for ref, cents, rest, at in order_rows:
        events.append(
            {
                "type": "order_placed",
                "at": at,
                "title": f"Order {ref} · KSh {cents / 100:,.0f}",
                "subtitle": rest,
            }
        )

    events.sort(key=lambda e: e["at"], reverse=True)
    return ok({"events": events[:limit]})


# ---------------------------------------------------------------------------
# Operator WRITE actions. Deliberately few, explicit, and every one audited.
# These are the only mutating endpoints in the admin surface.
# ---------------------------------------------------------------------------
async def _get_sub_or_404(db, restaurant_id: str) -> tuple[Restaurant, Subscription]:
    restaurant = await db.get(Restaurant, restaurant_id)
    if not restaurant:
        raise APIError("Restaurant not found", status=404)
    sub = (
        await db.execute(
            select(Subscription).where(Subscription.restaurant_id == restaurant_id)
        )
    ).scalar_one_or_none()
    if not sub:
        raise APIError("Restaurant has no subscription", status=404)
    return restaurant, sub


@router.post("/restaurants/{restaurant_id}/extend-trial")
async def extend_trial(
    restaurant_id: str,
    admin: PlatformAdmin,
    db: DbDep,
    request: Request,
    days: int = Query(default=14, ge=1, le=90),
):
    """Extend (or start) a trial by N days."""
    restaurant, sub = await _get_sub_or_404(db, restaurant_id)
    base = sub.trial_ends_at or datetime.utcnow()
    if base < datetime.utcnow():
        base = datetime.utcnow()
    sub.trial_ends_at = base + timedelta(days=days)
    sub.status = SubscriptionStatus.TRIALING
    restaurant.is_active = True

    await record_audit(
        db,
        action=AuditAction.ADMIN_TRIAL_EXTENDED,
        summary=f"Extended {restaurant.name}'s trial by {days} days",
        actor_id=admin.id,
        actor_email=admin.email,
        restaurant_id=restaurant.id,
        restaurant_name=restaurant.name,
        detail={"days": days, "new_trial_ends_at": sub.trial_ends_at.isoformat()},
        request=request,
    )
    await db.commit()
    return ok({"subscription": subscription_dict(sub)}, message=f"Trial extended {days} days")


@router.post("/restaurants/{restaurant_id}/comp-month")
async def comp_month(
    restaurant_id: str, admin: PlatformAdmin, db: DbDep, request: Request
):
    """Give a free month: mark active with a period 30 days out, no charge."""
    restaurant, sub = await _get_sub_or_404(db, restaurant_id)
    now = datetime.utcnow()
    start = sub.current_period_end if (sub.current_period_end and sub.current_period_end > now) else now
    sub.status = SubscriptionStatus.ACTIVE
    sub.current_period_start = now
    sub.current_period_end = start + timedelta(days=30)
    sub.failed_attempts = 0
    sub.next_retry_at = None
    restaurant.is_active = True

    await record_audit(
        db,
        action=AuditAction.ADMIN_MONTH_COMPED,
        summary=f"Comped a free month for {restaurant.name}",
        actor_id=admin.id,
        actor_email=admin.email,
        restaurant_id=restaurant.id,
        restaurant_name=restaurant.name,
        detail={"new_period_end": sub.current_period_end.isoformat()},
        request=request,
    )
    await db.commit()
    return ok({"subscription": subscription_dict(sub)}, message="Free month applied")


@router.post("/restaurants/{restaurant_id}/suspend")
async def suspend(
    restaurant_id: str, admin: PlatformAdmin, db: DbDep, request: Request
):
    """Manually suspend a restaurant (blocks POS; billing stays reachable)."""
    restaurant, sub = await _get_sub_or_404(db, restaurant_id)
    sub.status = SubscriptionStatus.SUSPENDED
    sub.next_retry_at = None
    restaurant.is_active = False

    await record_audit(
        db,
        action=AuditAction.ADMIN_SUSPENDED,
        summary=f"Manually suspended {restaurant.name}",
        actor_id=admin.id,
        actor_email=admin.email,
        restaurant_id=restaurant.id,
        restaurant_name=restaurant.name,
        request=request,
    )
    await db.commit()
    return ok({"subscription": subscription_dict(sub)}, message="Restaurant suspended")


@router.post("/restaurants/{restaurant_id}/reactivate")
async def reactivate(
    restaurant_id: str, admin: PlatformAdmin, db: DbDep, request: Request
):
    """Reactivate a suspended restaurant with a fresh 30-day active period."""
    restaurant, sub = await _get_sub_or_404(db, restaurant_id)
    now = datetime.utcnow()
    sub.status = SubscriptionStatus.ACTIVE
    sub.current_period_start = now
    sub.current_period_end = now + timedelta(days=30)
    sub.failed_attempts = 0
    sub.next_retry_at = None
    restaurant.is_active = True

    await record_audit(
        db,
        action=AuditAction.ADMIN_REACTIVATED,
        summary=f"Reactivated {restaurant.name}",
        actor_id=admin.id,
        actor_email=admin.email,
        restaurant_id=restaurant.id,
        restaurant_name=restaurant.name,
        request=request,
    )
    await db.commit()
    return ok({"subscription": subscription_dict(sub)}, message="Restaurant reactivated")


# ---------------------------------------------------------------------------
# Audit log viewer (read-only) — the forensic trail.
# ---------------------------------------------------------------------------
@router.get("/audit")
async def audit_log(
    admin: PlatformAdmin,
    db: DbDep,
    action: str | None = None,
    restaurant_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(AuditLog).order_by(AuditLog.created_at_idx.desc())
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if restaurant_id:
        stmt = stmt.where(AuditLog.restaurant_id == restaurant_id)
    stmt = stmt.limit(limit).offset(offset)

    rows = (await db.execute(stmt)).scalars().all()
    data = [
        {
            "id": r.id,
            "action": r.action,
            "summary": r.summary,
            "actor_email": r.actor_email,
            "restaurant_name": r.restaurant_name,
            "ip_address": r.ip_address,
            "at": r.created_at_idx,
        }
        for r in rows
    ]
    return ok({"events": data, "limit": limit, "offset": offset})
