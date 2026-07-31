"""Create the demo account Google Play reviewers sign in with.

Play's "App access" declaration requires working credentials for any app behind a
login. Two lines in Google's own wording decide how this account must be built:

  "We can't create new accounts"
      Signup here emails a 6-digit code and login is blocked until it's
      confirmed. A reviewer cannot receive that email, so the account must
      already exist with email_confirmed = True.

  "[We can't] use free-of-charge trials to review your app"
      require_subscription returns 402 once a subscription lapses, pausing
      ordering. A reviewer has no M-Pesa number and will not pay, so a trial or
      a 30-day period would strand them behind a paywall they cannot pass —
      which is a rejection. This account therefore gets an ACTIVE subscription
      with a period ending years out, which also keeps is_renewal_due() false so
      the dunning sweep never touches it.

Unlike seed.py this is SAFE TO RUN AGAINST PRODUCTION: it never deletes
anything, and re-running it updates the existing account rather than duplicating
it. It only seeds demo menu/orders when the restaurant has none, so a second run
won't pile up junk.

Usage:
    python create_review_account.py                       # defaults below
    python create_review_account.py --email r@x.co --password 'Str0ng!Pass#2026'
    python create_review_account.py --years 5             # subscription runway
"""
import argparse
import asyncio
import random
import sys
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.passwords import password_problem
from app.models import (
    Category,
    MenuItem,
    Order,
    OrderItem,
    Payment,
    Restaurant,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
)

DEFAULT_EMAIL = "play.review@karibupos.co.ke"
DEFAULT_PASSWORD = "PlayReview!2026#Karibu"
DEFAULT_RESTAURANT = "Karibu Demo — Kilimani"
DEFAULT_NAME = "Play Reviewer"
DEFAULT_PHONE = "0708374149"

# Small but representative: enough that every screen has content, few enough
# that the reviewer isn't scrolling. Prices in cents.
MENU = {
    "Main Dishes": ("restaurant", [
        ("Nyama Choma Platter", "Grilled goat meat with ugali & kachumbari", 180000, 25),
        ("Chicken & Chips", "Quarter chicken with crispy fries", 120000, 20),
        ("Ugali Sukuma Wiki", "Classic ugali with braised collard greens", 60000, 12),
        ("Pilau ya Nyama", "Spiced beef rice with a boiled egg", 85000, 18),
    ]),
    "Snacks": ("fast-food", [
        ("Samosa (2pc)", "Crispy beef samosas", 15000, 8),
        ("Chapati", "Soft layered flatbread", 8000, 6),
    ]),
    "Drinks": ("cafe", [
        ("Chai ya Maziwa", "Kenyan milk tea", 8000, 5),
        ("Soda (300ml)", "Assorted soft drinks", 10000, 2),
    ]),
}


async def main() -> int:
    p = argparse.ArgumentParser(description="Create/refresh the Play review account")
    p.add_argument("--email", default=DEFAULT_EMAIL)
    p.add_argument("--password", default=DEFAULT_PASSWORD)
    p.add_argument("--name", default=DEFAULT_NAME)
    p.add_argument("--restaurant", default=DEFAULT_RESTAURANT)
    p.add_argument("--years", type=int, default=5,
                   help="Subscription runway. Must outlast review AND any "
                        "re-review of a future release.")
    args = p.parse_args()

    email = args.email.strip().lower()

    # The app's own password rules apply — a reviewer account that can't log in
    # because the password was rejected at creation is the worst outcome here.
    problem = password_problem(args.password)
    if problem:
        print(f"Password rejected: {problem}", file=sys.stderr)
        return 1

    now = datetime.utcnow()
    period_end = now + timedelta(days=365 * args.years)

    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(func.lower(User.email) == email))
        ).scalar_one_or_none()

        if user:
            restaurant = await db.get(Restaurant, user.restaurant_id)
            print(f"Updating existing account {email}")
        else:
            restaurant = Restaurant(
                name=args.restaurant, billing_phone=DEFAULT_PHONE, is_active=True
            )
            db.add(restaurant)
            await db.flush()
            user = User(
                full_name=args.name,
                email=email,
                role=UserRole.MANAGER,
                phone=DEFAULT_PHONE,
                restaurant_id=restaurant.id,
            )
            db.add(user)
            print(f"Creating account {email}")

        # --- The two things Play's wording forces -------------------------
        user.set_password(args.password)
        user.email_confirmed = True   # reviewers cannot receive the OTP email
        user.is_active = True
        # Invalidate any older tokens so the fresh password is the only way in.
        user.token_version = (user.token_version or 0) + 1
        restaurant.is_active = True

        sub = (
            await db.execute(
                select(Subscription).where(Subscription.restaurant_id == restaurant.id)
            )
        ).scalar_one_or_none()
        if not sub:
            sub = Subscription(
                restaurant_id=restaurant.id,
                price_cents=settings.SUBSCRIPTION_PRICE_CENTS,
                currency=settings.SUBSCRIPTION_CURRENCY,
            )
            db.add(sub)

        # ACTIVE with a period ending years out: has_access is true, and
        # is_renewal_due() stays false so the billing sweep never charges it and
        # never walks it into past_due -> suspended.
        sub.status = SubscriptionStatus.ACTIVE
        sub.trial_ends_at = now - timedelta(days=1)   # trial already behind us
        sub.current_period_start = now - timedelta(days=1)
        sub.current_period_end = period_end
        sub.failed_attempts = 0
        sub.next_retry_at = None

        await db.flush()

        # --- Demo content, only if this tenant is empty --------------------
        existing_items = (
            await db.execute(
                select(func.count())
                .select_from(MenuItem)
                .where(MenuItem.restaurant_id == restaurant.id)
            )
        ).scalar() or 0

        if existing_items:
            print(f"  menu already has {existing_items} item(s) — leaving it alone")
            lookup = {}
        else:
            lookup = {}
            for sort_order, (cat_name, (icon, items)) in enumerate(MENU.items()):
                cat = Category(
                    restaurant_id=restaurant.id, name=cat_name,
                    icon=icon, sort_order=sort_order,
                )
                db.add(cat)
                await db.flush()
                for name, desc, cents, prep in items:
                    item = MenuItem(
                        restaurant_id=restaurant.id, category_id=cat.id,
                        name=name, description=desc, price_cents=cents,
                        prep_minutes=prep, is_available=True,
                    )
                    db.add(item)
                    lookup[name] = item
            await db.flush()
            print(f"  seeded {len(lookup)} menu items across {len(MENU)} categories")

        await db.commit()

        orders = (
            await db.execute(
                select(func.count())
                .select_from(Order)
                .where(Order.restaurant_id == restaurant.id)
            )
        ).scalar() or 0
        if orders:
            print(f"  {orders} order(s) already present — not adding more")
        elif lookup:
            # One live order and one settled, so Orders, Dashboard and Analytics
            # all have something to render.
            await _demo_order(db, restaurant, user, lookup,
                              "4", [("Nyama Choma Platter", 1), ("Chai ya Maziwa", 2)],
                              paid=False)
            await _demo_order(db, restaurant, user, lookup,
                              "7", [("Chicken & Chips", 2), ("Soda (300ml)", 2)],
                              paid=True)
            print("  seeded 2 demo orders (1 in progress, 1 paid)")

    print("\n" + "=" * 62)
    print("Paste these into Play Console -> App access -> Sign-in details")
    print("=" * 62)
    print(f"  Username / email : {email}")
    print(f"  Password         : {args.password}")
    print(f"  Subscription     : ACTIVE until {period_end:%d %b %Y}")
    print("=" * 62)
    print("This account is pre-confirmed (no email code needed) and its")
    print("subscription outlasts review, so no paywall can block a reviewer.")
    return 0


async def _demo_order(db, restaurant, user, lookup, table, lines, paid):
    order = Order(
        reference=f"ORD-{random.randint(1000, 9999)}",
        table_number=table,
        server_id=user.id,
        restaurant_id=restaurant.id,
        status="served" if paid else "preparing",
    )
    db.add(order)
    for name, qty in lines:
        item = lookup[name]
        order.items.append(
            OrderItem(
                menu_item_id=item.id,
                name_snapshot=item.name,
                unit_price_cents=item.price_cents,
                quantity=qty,
            )
        )
    order.recalculate()
    if paid:
        order.payments.append(
            Payment(method="mpesa", amount_cents=order.total_cents, reference="DEMO123")
        )
        await db.flush()
        order.sync_payment_status()
        order.status = "completed"
    await db.commit()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
