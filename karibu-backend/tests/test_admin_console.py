"""The platform console.

Mostly about what the operator's own view counts as a customer — the admin
account's HQ placeholder is not one, and reporting it as such overstates the
business to the person least placed to notice.
"""
import uuid

from tests.conftest import PASSWORD, register_and_confirm


def make_admin(client, db_email=None):
    """Promote a fresh account the way create_admin.py does."""
    import asyncio

    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models import User

    async def promote(email):
        async with AsyncSessionLocal() as db:
            u = (await db.execute(select(User).where(User.email == email))).scalar_one()
            u.is_platform_admin = True
            await db.commit()

    return promote


def test_admin_endpoints_are_invisible_to_a_normal_manager(client, owner):
    """404, not 403 — probing must not confirm the routes exist."""
    for path in ("/api/admin/overview", "/api/admin/restaurants"):
        assert client.get(path, headers=owner["headers"]).status_code == 404, path


def test_the_console_lists_customers(client, mail_log, owner, monkeypatch):
    import asyncio

    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models import User

    async def promote():
        async with AsyncSessionLocal() as db:
            u = (await db.execute(
                select(User).where(User.email == owner["email"])
            )).scalar_one()
            u.is_platform_admin = True
            await db.commit()

    asyncio.get_event_loop().run_until_complete(promote())

    r = client.get("/api/admin/restaurants", headers=owner["headers"])
    assert r.status_code == 200, r.text
    names = [x["name"] for x in r.json()["data"]["restaurants"]]
    assert "Test Kitchen" in names
    # Every listed restaurant is a customer, so every one has a subscription —
    # which is what makes the actions on it meaningful.
    assert all(x["subscription"] for x in r.json()["data"]["restaurants"])


def test_a_restaurant_without_a_subscription_is_not_a_customer(client, owner):
    """The HQ row create_admin.py makes has no subscription. It must not be
    counted, or the operator's own numbers overstate the business."""
    import asyncio

    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models import Restaurant, User

    async def setup():
        async with AsyncSessionLocal() as db:
            u = (await db.execute(
                select(User).where(User.email == owner["email"])
            )).scalar_one()
            u.is_platform_admin = True
            db.add(Restaurant(name="Karibu Platform HQ"))
            await db.commit()

    asyncio.get_event_loop().run_until_complete(setup())

    listing = client.get("/api/admin/restaurants", headers=owner["headers"]).json()["data"]
    assert "Karibu Platform HQ" not in [x["name"] for x in listing["restaurants"]]

    overview = client.get("/api/admin/overview", headers=owner["headers"]).json()["data"]
    assert overview["restaurants_total"] == len(listing["restaurants"])
