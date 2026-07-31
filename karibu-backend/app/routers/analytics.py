"""Analytics routes: dashboard pulse + sales report. Tenant-scoped via joins.

Performance note: every aggregate here is computed IN THE DATABASE (SUM /
GROUP BY / LIMIT against indexed columns), not by loading rows into Python.
That keeps these endpoints O(result size) instead of O(row count) — the
difference between milliseconds and seconds once a busy restaurant has months
of orders. The one Python-side pass (daily revenue series) reads bare
(timestamp, cents) tuples, not ORM objects, and is bounded by the 90-day cap.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.dependencies import DbDep, SubscribedUser, require_roles
from app.core.serializers import order_dict
from app.models import (
    Debt, DebtStatus, Order, OrderItem, OrderStatus, Payment, Restaurant, User,
    UserRole,
)
from app.schemas.common import ok
from app.services.reports import Report, money, render

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# Raw enum keys are what the database calls them; a report is read by a person.
PAYMENT_LABELS = {
    "mpesa": "M-Pesa",
    "cash": "Cash",
    "card": "Card",
    "debt": "On credit",
}


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

    # ── Hourly takings for today ────────────────────────────────────────────
    # Bucketed in Python from bare (timestamp, cents) tuples rather than with a
    # date_trunc, because the expression differs across Postgres and SQLite and
    # a day of payments is a trivially small read.
    hourly_res = await db.execute(
        select(Payment.received_at, Payment.amount_cents)
        .join(Order, Payment.order_id == Order.id)
        .where(
            Order.restaurant_id == rid,
            Payment.received_at >= today_start,
            Payment.received_at < today_end,
        )
    )
    buckets: dict[int, int] = {}
    for received_at, cents in hourly_res.all():
        buckets[received_at.hour] = buckets.get(received_at.hour, 0) + (cents or 0)
    # A fixed 7am–9pm window so the chart has a stable x-axis all day instead of
    # growing a column each hour — and so a quiet hour reads as a quiet hour
    # rather than vanishing.
    hourly_sales = [
        {"hour": h, "amount": round(buckets.get(h, 0) / 100, 2)} for h in range(7, 22)
    ]

    # ── 12-day trend, for the sparkline ─────────────────────────────────────
    trend_start = today_start - timedelta(days=11)
    trend_res = await db.execute(
        select(Payment.received_at, Payment.amount_cents)
        .join(Order, Payment.order_id == Order.id)
        .where(Order.restaurant_id == rid, Payment.received_at >= trend_start)
    )
    day_totals: dict[str, int] = {}
    for received_at, cents in trend_res.all():
        key = received_at.date().isoformat()
        day_totals[key] = day_totals.get(key, 0) + (cents or 0)
    sales_trend = [
        round(day_totals.get((trend_start + timedelta(days=i)).date().isoformat(), 0) / 100, 2)
        for i in range(12)
    ]

    # ── Top items today ─────────────────────────────────────────────────────
    top_res = await db.execute(
        select(
            OrderItem.name_snapshot,
            func.sum(OrderItem.quantity),
            func.sum(OrderItem.unit_price_cents * OrderItem.quantity),
        )
        .join(Order, OrderItem.order_id == Order.id)
        .where(
            Order.restaurant_id == rid,
            Order.created_at >= today_start,
            Order.created_at < today_end,
            Order.status != OrderStatus.CANCELLED,
        )
        .group_by(OrderItem.name_snapshot)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(5)
    )
    top_items = [
        {"name": name, "qty": int(qty or 0), "sales": round((cents or 0) / 100, 2)}
        for name, qty, cents in top_res.all()
    ]

    # ── Who served what, today ──────────────────────────────────────────────
    staff_res = await db.execute(
        select(
            User.full_name,
            User.role,
            func.count(func.distinct(Order.id)),
            func.coalesce(func.sum(Payment.amount_cents), 0),
        )
        .select_from(Order)
        .join(User, Order.server_id == User.id)
        .outerjoin(Payment, Payment.order_id == Order.id)
        .where(
            Order.restaurant_id == rid,
            Order.created_at >= today_start,
            Order.created_at < today_end,
            Order.status != OrderStatus.CANCELLED,
        )
        .group_by(User.id, User.full_name, User.role)
        .order_by(func.coalesce(func.sum(Payment.amount_cents), 0).desc())
        .limit(6)
    )
    staff_today = [
        {
            "name": name,
            "role": role,
            "orders": int(orders or 0),
            "sales": round((cents or 0) / 100, 2),
        }
        for name, role, orders, cents in staff_res.all()
    ]

    # ── Average time from placed to completed, today ────────────────────────
    # Returned in minutes, or null when nothing has completed yet — the client
    # must be able to tell "no data" from "zero minutes".
    done_res = await db.execute(
        select(Order.created_at, Order.updated_at).where(
            Order.restaurant_id == rid,
            Order.status == OrderStatus.COMPLETED,
            Order.created_at >= today_start,
            Order.created_at < today_end,
        )
    )
    spans = [
        (updated - created).total_seconds() / 60
        for created, updated in done_res.all()
        if updated and created and updated >= created
    ]
    avg_prep_minutes = round(sum(spans) / len(spans)) if spans else None

    payload = {
        "active_orders": active_orders,
        "completed_orders": completed_today,
        "live_orders": [order_dict(o, detailed=False) for o in live],
        "avg_prep_minutes": avg_prep_minutes,
        # Tells the client which half it is looking at, so it can lay the page
        # out rather than guess from missing keys.
        "shows_money": user.role in UserRole.MANAGERS,
    }

    # THE DASHBOARD IS THE HOME SCREEN FOR EVERY ROLE, so it stays reachable by
    # a waiter who needs the live order list. What it must not do is hand them
    # the day's takings, the 12-day trend and every colleague's sales figures.
    # Those are stripped here rather than hidden in the UI — the API is what
    # decides who sees money.
    if user.role in UserRole.MANAGERS:
        payload.update(
            {
                "total_sales": round(today_sales / 100, 2),
                "sales_change_pct": change_pct,
                "hourly_sales": hourly_sales,
                "sales_trend": sales_trend,
                "top_items": top_items,
                "staff_today": staff_today,
            }
        )

    return ok(payload)


@router.get("/sales", dependencies=[Depends(require_roles(*UserRole.MANAGERS))])
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


@router.get("/sales.csv", dependencies=[Depends(require_roles(*UserRole.MANAGERS))])
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


@router.get("/sales.pdf", dependencies=[Depends(require_roles(*UserRole.MANAGERS))])
async def sales_pdf(
    user: SubscribedUser,
    db: DbDep,
    days: int = Query(default=30, ge=1, le=366),
):
    """The sales report as a PDF the owner can send to an accountant or a bank.

    Deliberately a SUMMARY, not the row dump that sales.csv already provides.
    A PDF is read, not filtered — if someone wants every line to sort and pivot,
    the CSV is the right file and this one would just be a worse spreadsheet.
    """
    rid = user.restaurant_id
    window_start = (datetime.utcnow() - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    gross_res = await db.execute(
        select(func.coalesce(func.sum(Payment.amount_cents), 0))
        .join(Order, Payment.order_id == Order.id)
        .where(Order.restaurant_id == rid, Payment.received_at >= window_start)
    )
    gross_cents = gross_res.scalar() or 0

    count_res = await db.execute(
        select(func.count())
        .select_from(Order)
        .where(
            Order.restaurant_id == rid,
            Order.created_at >= window_start,
            Order.status != OrderStatus.CANCELLED,
        )
    )
    order_count = count_res.scalar() or 0

    split_res = await db.execute(
        select(Payment.method, func.sum(Payment.amount_cents))
        .join(Order, Payment.order_id == Order.id)
        .where(Order.restaurant_id == rid, Payment.received_at >= window_start)
        .group_by(Payment.method)
        .order_by(func.sum(Payment.amount_cents).desc())
    )
    method_split = split_res.all()

    top_res = await db.execute(
        select(
            OrderItem.name_snapshot,
            func.sum(OrderItem.quantity),
            func.sum(OrderItem.unit_price_cents * OrderItem.quantity),
        )
        .join(Order, OrderItem.order_id == Order.id)
        .where(
            Order.restaurant_id == rid,
            Order.created_at >= window_start,
            Order.status != OrderStatus.CANCELLED,
        )
        .group_by(OrderItem.name_snapshot)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(10)
    )
    top_items = top_res.all()

    series_res = await db.execute(
        select(Payment.received_at, Payment.amount_cents)
        .join(Order, Payment.order_id == Order.id)
        .where(Order.restaurant_id == rid, Payment.received_at >= window_start)
    )
    daily: dict[str, int] = {
        (window_start + timedelta(days=i)).strftime("%Y-%m-%d"): 0 for i in range(days)
    }
    for received_at, cents in series_res.all():
        key = received_at.strftime("%Y-%m-%d")
        if key in daily:
            daily[key] += cents or 0

    avg_cents = round(gross_cents / order_count) if order_count else 0
    period = (
        f"{window_start:%d %b %Y} - {datetime.utcnow():%d %b %Y}"
        f" ({days} day{'s' if days != 1 else ''})"
    )

    # The report is headed with the restaurant's real name. It used to use
    # a free-text per-user "branch" label, which nothing scoped by.
    restaurant = await db.get(Restaurant, user.restaurant_id)
    restaurant_name = restaurant.name if restaurant else "Karibu POS"
    pdf = Report("Sales report", restaurant_name, period)
    pdf.alias_nb_pages()
    pdf.add_page()

    pdf.stat_row([
        ("Gross revenue", money(gross_cents)),
        ("Orders", f"{order_count:,}"),
        ("Average order", money(avg_cents)),
    ])

    pdf.section("How customers paid")
    pdf.table(
        ["Method", "Amount", "Share"],
        [
            [
                PAYMENT_LABELS.get(method, method.title()),
                money(cents or 0),
                f"{(cents or 0) / gross_cents * 100:.0f}%" if gross_cents else "-",
            ]
            for method, cents in method_split
        ],
        widths=[80, 60, 46],
        align=["L", "R", "R"],
        empty_message="No payments were recorded in this period.",
    )

    pdf.section("Best sellers")
    pdf.table(
        ["#", "Item", "Sold", "Revenue"],
        [
            [str(i + 1), name, str(int(qty or 0)), money(cents or 0)]
            for i, (name, qty, cents) in enumerate(top_items)
        ],
        widths=[12, 108, 26, 40],
        align=["L", "L", "R", "R"],
        empty_message="Nothing was sold in this period.",
    )

    pdf.section("Daily revenue")
    pdf.table(
        ["Date", "Revenue"],
        [[datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b"), money(c)]
         for d, c in daily.items()],
        widths=[100, 86],
        align=["L", "R"],
    )

    filename = f"karibu-sales-{datetime.utcnow():%Y%m%d}.pdf"
    return Response(
        content=render(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/accountability", dependencies=[Depends(require_roles(*UserRole.MANAGERS))])
async def accountability(
    user: SubscribedUser,
    db: DbDep,
    days: int = Query(default=30, ge=1, le=366),
):
    """Who is carrying unpaid money, by name.

    Two different exposures per person, kept apart on purpose:

      UNPAID ORDERS  — served, still not fully paid, not cancelled. Often just
                       a table still eating; only a worry once it is old.
      CREDIT GIVEN   — debts they authorised that are still outstanding. This
                       is the deliberate decision to let food leave unpaid, and
                       it is the number an owner actually wants a name against.

    Summing them into one "owed" figure would merge a table mid-service with a
    customer who has not paid in three weeks, and the whole point is to tell
    those apart.
    """
    rid = user.restaurant_id
    since = datetime.utcnow() - timedelta(days=days)

    # Outstanding credit PER ORDER, needed before the unpaid pass.
    #
    # Settling an order as credit deliberately creates a Debt and no Payment —
    # credit is not revenue until collected — so the order's amount_paid stays
    # at zero. Without subtracting it here the same money is reported twice:
    # once as an unpaid order and again as outstanding credit, and the two
    # columns this endpoint exists to separate would both contain it.
    credit_res = await db.execute(
        select(Debt.order_id, func.sum(Debt.amount_cents - Debt.paid_cents))
        .where(Debt.restaurant_id == rid, Debt.status == DebtStatus.OUTSTANDING)
        .group_by(Debt.order_id)
    )
    credit_on_order = {oid: cents or 0 for oid, cents in credit_res.all()}

    # Unpaid orders per server. amount_paid_cents is a Python property, so the
    # shortfall is computed from the columns rather than filtered in SQL.
    orders_res = await db.execute(
        select(Order)
        .where(
            Order.restaurant_id == rid,
            Order.created_at >= since,
            Order.status != OrderStatus.CANCELLED,
        )
        .options(selectinload(Order.payments), selectinload(Order.server))
    )
    people: dict[str, dict] = {}

    def slot(name: str | None) -> dict:
        # Orders predating server tracking have no name. They are grouped
        # honestly rather than dropped — a total that silently excludes them
        # would not reconcile against the debts page.
        key = name or "Unattributed"
        return people.setdefault(
            key,
            {
                "name": key,
                "orders_served": 0,
                "sales": 0,
                "unpaid_orders": 0,
                "unpaid_value": 0,
                "credit_given": 0,
                "credit_outstanding": 0,
            },
        )

    for o in orders_res.scalars().all():
        row = slot(o.server.full_name if o.server else None)
        row["orders_served"] += 1
        row["sales"] += o.amount_paid_cents
        shortfall = o.total_cents - o.amount_paid_cents - credit_on_order.get(o.id, 0)
        if shortfall > 0:
            row["unpaid_orders"] += 1
            row["unpaid_value"] += shortfall

    debts_res = await db.execute(
        select(Debt)
        .where(Debt.restaurant_id == rid, Debt.created_at_idx >= since)
        .options(
            selectinload(Debt.recorded_by),
            selectinload(Debt.order).selectinload(Order.server),
        )
    )
    for d in debts_res.scalars().all():
        # Attributed to whoever authorised the credit, falling back to whoever
        # served the order for debts created before that was recorded.
        who = (
            d.recorded_by.full_name
            if d.recorded_by
            else (d.order.server.full_name if d.order and d.order.server else None)
        )
        row = slot(who)
        row["credit_given"] += 1
        row["credit_outstanding"] += d.outstanding_cents

    rows = sorted(
        people.values(),
        key=lambda r: (r["credit_outstanding"], r["unpaid_value"]),
        reverse=True,
    )
    for r in rows:
        for money_key in ("sales", "unpaid_value", "credit_outstanding"):
            r[money_key] = round(r[money_key] / 100, 2)

    return ok(
        {
            "range_days": days,
            "staff": rows,
            "total_unpaid": round(sum(r["unpaid_value"] for r in rows), 2),
            "total_credit_outstanding": round(
                sum(r["credit_outstanding"] for r in rows), 2
            ),
        }
    )
