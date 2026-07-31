"""Seed the database with a demo restaurant, user, and Kenyan menu.

Run with:  python seed.py
Safe to re-run — clears and rebuilds demo data.
"""
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, Base, engine
from app.models import (
    BillingCharge,
    Category,
    MenuItem,
    Order,
    OrderItem,
    Payment,
    ProcessedCallback,
    Restaurant,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
)
from app.core.config import settings

DEMO_RESTAURANT = "Karibu Kitchen — Kilimani"
DEMO_USER = {
    "full_name": "Baraka Otieno",
    "email": "demo@karibupos.co.ke",
    "password": "karibu12345",
    "role": UserRole.MANAGER,
    "billing_phone": "0708374149",
}

MENU = {
    "Main Dishes": {
        "icon": "restaurant",
        "items": [
            ("Nyama Choma Platter", "Grilled goat meat with ugali & kachumbari", 1800, 25),
            ("Chicken & Chips", "Quarter chicken with crispy fries", 1200, 20),
            ("Ugali Sukuma Wiki", "Classic ugali with braised collard greens", 600, 12),
            ("Pilau ya Nyama", "Spiced beef rice with a boiled egg", 850, 18),
            ("Fish Fry (Tilapia)", "Whole fried tilapia with ugali", 1400, 22),
        ],
    },
    "Snacks": {
        "icon": "bakery_dining",
        "items": [
            ("Samosa (2pc)", "Crispy beef samosas", 150, 8),
            ("Chapati", "Soft layered flatbread", 80, 6),
            ("Bhajia", "Spiced potato fritters", 300, 10),
            ("Smokie Pasua", "Sausage with kachumbari", 120, 5),
        ],
    },
    "Beverages": {
        "icon": "local_cafe",
        "items": [
            ("Chai ya Maziwa", "Kenyan milk tea", 100, 5),
            ("Fresh Passion Juice", "Cold-pressed passion fruit", 250, 4),
            ("Soda 500ml", "Assorted soft drinks", 120, 2),
            ("Dawa", "Honey, lemon & ginger", 200, 6),
        ],
    },
}


async def seed():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        print("Resetting demo data...")
        for model in (Payment, OrderItem, Order, MenuItem, Category, BillingCharge, ProcessedCallback, Subscription, User, Restaurant):
            await db.execute(delete(model))
        await db.commit()

        now = datetime.utcnow()
        restaurant = Restaurant(name=DEMO_RESTAURANT, billing_phone=DEMO_USER["billing_phone"], is_active=True)
        db.add(restaurant)
        await db.flush()

        sub = Subscription(
            restaurant_id=restaurant.id,
            status=SubscriptionStatus.ACTIVE,
            price_cents=settings.SUBSCRIPTION_PRICE_CENTS,
            currency=settings.SUBSCRIPTION_CURRENCY,
            trial_ends_at=now - timedelta(days=1),
            current_period_start=now - timedelta(days=3),
            current_period_end=now + timedelta(days=27),
        )
        db.add(sub)

        user = User(
            full_name=DEMO_USER["full_name"],
            email=DEMO_USER["email"],
            role=DEMO_USER["role"],
            phone=DEMO_USER["billing_phone"],
            restaurant_id=restaurant.id,
        )
        user.set_password(DEMO_USER["password"])
        user.email_confirmed = True  # demo account is pre-confirmed
        db.add(user)

        # Platform admin (SaaS operator) — attached to an HQ tenant row.
        hq = Restaurant(name="Karibu Platform HQ", is_active=True)
        db.add(hq)
        await db.flush()
        admin = User(
            full_name="Katiano (Platform Admin)",
            email="admin@karibupos.co.ke",
            role=UserRole.MANAGER,
            restaurant_id=hq.id,
            is_platform_admin=True,
        )
        admin.set_password("karibu-admin-99")
        admin.email_confirmed = True
        db.add(admin)

        item_lookup = {}
        for idx, (cat_name, cfg) in enumerate(MENU.items()):
            category = Category(name=cat_name, icon=cfg["icon"], sort_order=idx, restaurant_id=restaurant.id)
            db.add(category)
            await db.flush()
            for name, desc, price_kes, prep in cfg["items"]:
                item = MenuItem(
                    name=name, description=desc, price_cents=price_kes * 100,
                    prep_minutes=prep, category_id=category.id, restaurant_id=restaurant.id,
                )
                db.add(item)
                await db.flush()
                item_lookup[name] = item

        await db.commit()

        await _demo_order(db, restaurant, user, item_lookup, "12", [("Nyama Choma Platter", 1), ("Chai ya Maziwa", 2)], paid=True)
        await _demo_order(db, restaurant, user, item_lookup, "5", [("Chicken & Chips", 2), ("Soda 500ml", 2)], paid=False)

        print("Seeded 1 restaurant, 1 user, 3 categories, 13 items, 2 orders, 1 subscription.")
        print(f"\nLogin with:  {DEMO_USER['email']}  /  {DEMO_USER['password']}")
        print("Platform admin:  admin@karibupos.co.ke  /  karibu-admin-99")


async def _demo_order(db, restaurant, user, lookup, table, lines, paid):
    import random

    order = Order(
        reference=f"ORD-{random.randint(1000, 9999)}",
        table_number=table, server_id=user.id, restaurant_id=restaurant.id,
        status="served" if paid else "preparing",
    )
    db.add(order)
    for name, qty in lines:
        item = lookup[name]
        order.items.append(
            OrderItem(menu_item_id=item.id, name_snapshot=item.name, unit_price_cents=item.price_cents, quantity=qty)
        )
    order.recalculate()
    if paid:
        order.payments.append(Payment(method="mpesa", amount_cents=order.total_cents, reference="QGH7XYZ12"))
        await db.flush()
        order.sync_payment_status()
        order.status = "completed"
    await db.commit()


if __name__ == "__main__":
    asyncio.run(seed())
