"""Analytics routes: dashboard pulse + sales report. Tenant-scoped via joins.

Performance note: every aggregate here is computed IN THE DATABASE (SUM /
GROUP BY / LIMIT against indexed columns), not by loading rows into Python.
That keeps these endpoints O(result size) instead of O(row count) — the
difference between milliseconds and seconds once a busy restaurant has months
of orders. The one Python-side pass (daily revenue series) reads bare
(timestamp, cents) tuples, not ORM objects, and is bounded by the 90-day cap.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.dependencies import DbDep, SubscribedUser
from app.core.serializers import order_dict
from app.models import Order, OrderItem, OrderStatus, Payment
from app.schemas.common import ok

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _day_bounds(day: datetime):
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


@router.get("/dashboard")
async def dashboard(user: SubscribedUser, db: DbDep):
    rid = user.restaurant_id
    today_start, today_end = _day_bounds(datetime.utcnow())
    yday_start, yday_end = _day_bounds(datetime.utcnow() - timedelta(days=1))

    async def sales_between(start, end) -> int:
        result = await db.execute(
            select(func.coalesce(func.sum(Payment.amount_cents), 0))
            .join(Order, Payment.order_id == Order.id)
            .where(
                Order.restaurant_id == rid,
                Payment.received_at >= start,
                Payment.received_at < end,
            )
        )
        return result.scalar() or 0

    today_sales = await sales_between(today_start, today_end)
    yday_sales = await sales_between(yday_start, yday_end)
    change_pct = round((today_sales - yday_sales) / yday_sales * 100, 1) if yday_sales > 0 else 0.0

    active_res = await db.execute(
        select(func.count())
        .select_from(Order)
        .where(Order.restaurant_id == rid, Order.status.in_(OrderStatus.OPEN))
    )
    active_orders = active_res.scalar() or 0

    completed_res = await db.execute(
        select(func.count())
        .select_from(Order)
        .where(
            Order.restaurant_id == rid,
            Order.status == OrderStatus.COMPLETED,
            Order.created_at >= today_start,
            Order.created_at < today_end,
        )
    )
    completed_today = completed_res.scalar() or 0

    live_res = await db.execute(
        select(Order)
        .where(Order.restaurant_id == rid, Order.status.in_(OrderStatus.OPEN))
        .options(selectinload(Order.items), selectinload(Order.payments), selectinload(Order.server))
        .order_by(Order.created_at.desc())
        .limit(6)
    )
    live = live_res.scalars().all()

    return ok(
        {
            "total_sales": round(today_sales / 100, 2),
            "sales_change_pct": change_pct,
            "active_orders": active_orders,
            "completed_orders": completed_today,
            "live_orders": [order_dict(o, detailed=False) for o in live],
        }
    )


@router.get("/sales")
async def sales_report(user: SubscribedUser, db: DbDep, days: int = Query(default=7, ge=1, le=90)):
    rid = user.restaurant_id
    window_start = (datetime.utcnow() - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # Gross revenue: one SUM, computed by the DB.
    gross_res = await db.execute(
        select(func.coalesce(func.sum(Payment.amount_cents), 0))
        .join(Order, Payment.order_id == Order.id)
        .where(Order.restaurant_id == rid, Payment.received_at >= window_start)
    )
    gross_cents = gross_res.scalar() or 0

    # Payment-method split: GROUP BY in the DB (one row per method).
    split_res = await db.execute(
        select(Payment.method, func.sum(Payment.amount_cents))
        .join(Order, Payment.order_id == Order.id)
        .where(Order.restaurant_id == rid, Payment.received_at >= window_start)
        .group_by(Payment.method)
    )
    method_split = {method: total for method, total in split_res.all()}

    # Completed-order count: SQL count.
    count_res = await db.execute(
        select(func.count())
        .select_from(Order)
        .where(
            Order.restaurant_id == rid,
            Order.created_at >= window_start,
            Order.status == OrderStatus.COMPLETED,
        )
    )
    order_count = count_res.scalar() or 0
    avg_order = round(gross_cents / order_count / 100, 2) if order_count else 0

    # Top sellers: GROUP BY + ORDER BY + LIMIT in the DB (five rows back).
    top_res = await db.execute(
        select(OrderItem.name_snapshot, func.sum(OrderItem.quantity).label("qty"))
        .join(Order, OrderItem.order_id == Order.id)
        .where(Order.restaurant_id == rid, Order.created_at >= window_start)
        .group_by(OrderItem.name_snapshot)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(5)
    )
    top_sellers = [{"name": name, "quantity": int(qty)} for name, qty in top_res.all()]

    # Daily revenue series: bare (timestamp, cents) tuples — no ORM hydration —
    # bucketed in Python for dialect-safe date handling (SQLite dev / PG prod).
    series_res = await db.execute(
        select(Payment.received_at, Payment.amount_cents)
        .join(Order, Payment.order_id == Order.id)
        .where(Order.restaurant_id == rid, Payment.received_at >= window_start)
    )
    daily = {
        (window_start + timedelta(days=i)).strftime("%Y-%m-%d"): 0 for i in range(days)
    }
    for received_at, cents in series_res.all():
        key = received_at.strftime("%Y-%m-%d")
        if key in daily:
            daily[key] += cents
    revenue_series = [{"date": k, "revenue": round(v / 100, 2)} for k, v in daily.items()]

    return ok(
        {
            "range_days": days,
            "gross_revenue": round(gross_cents / 100, 2),
            "order_count": order_count,
            "average_order_value": avg_order,
            "payment_methods": {m: round(c / 100, 2) for m, c in method_split.items()},
            "top_sellers": top_sellers,
            "revenue_series": revenue_series,
        }
    )


@router.get("/sales.csv")
async def sales_csv(
    user: SubscribedUser,
    db: DbDep,
    days: int = Query(default=30, ge=1, le=366),
):
    """Export completed-order sales as CSV for the restaurant's accountant.

    One row per order with totals and payment method(s). Streamed as a file
    download. Owner/manager only would be ideal, but any active staff can pull
    their own restaurant's figures; tenant scoping keeps it to their data.
    """
    import csv
    import io

    from fastapi.responses import StreamingResponse
    from sqlalchemy.orm import selectinload

    window_start = (datetime.utcnow() - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    result = await db.execute(
        select(Order)
        .where(
            Order.restaurant_id == user.restaurant_id,
            Order.created_at >= window_start,
        )
        .options(selectinload(Order.payments))
        .order_by(Order.created_at)
    )
    orders = result.scalars().all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Date",
            "Time",
            "Reference",
            "Type",
            "Status",
            "Payment status",
            "Subtotal (KES)",
            "Discount (KES)",
            "Total (KES)",
            "Amount paid (KES)",
            "Payment methods",
        ]
    )
    for o in orders:
        methods = ", ".join(sorted({p.method for p in o.payments})) or "-"
        writer.writerow(
            [
                o.created_at.strftime("%Y-%m-%d"),
                o.created_at.strftime("%H:%M"),
                o.reference,
                o.order_type,
                o.status,
                o.payment_status,
                f"{o.subtotal_cents / 100:.2f}",
                f"{o.discount_cents / 100:.2f}",
                f"{o.total_cents / 100:.2f}",
                f"{o.amount_paid_cents / 100:.2f}",
                methods,
            ]
        )

    buffer.seek(0)
    filename = f"karibu-sales-{datetime.utcnow():%Y%m%d}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
