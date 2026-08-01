"""Settling a charge the moment the gateway knows.

Waiting for a webhook that may be lost, then a scheduler sweep ten minutes
later, is what made a paid subscription sit on "processing" — and what made
people pay twice.
"""
import uuid

import pytest

from app.models import BillingCharge, ChargeStatus
from app.services import billing, paystack


def _sub_id(client, owner):
    r = client.get("/api/billing/subscription", headers=owner["headers"])
    return r.json()["data"]["subscription"]["id"]


def test_the_price_follows_the_configured_one(client, owner):
    """A stamped price meant changing the setting moved nothing."""
    from app.core.config import settings

    sub = client.get("/api/billing/subscription",
                     headers=owner["headers"]).json()["data"]["subscription"]
    assert sub["price"] == settings.SUBSCRIPTION_PRICE_CENTS / 100


def test_verify_is_scoped_to_the_caller(client, mail_log, owner):
    from tests.conftest import register_and_confirm

    _, _, other = register_and_confirm(client, mail_log, restaurant="Rival Cafe")
    h = {"Authorization": f"Bearer {other['tokens']['access_token']}"}
    r = client.post(f"/api/billing/charges/{uuid.uuid4()}/verify", headers=h)
    assert r.status_code == 404


def test_verify_on_an_unknown_charge_is_404(client, owner):
    r = client.post(f"/api/billing/charges/{uuid.uuid4()}/verify",
                    headers=owner["headers"])
    assert r.status_code == 404


@pytest.mark.parametrize(
    "gateway_status,expected",
    [
        ("success", ChargeStatus.SUCCESS),
        ("failed", ChargeStatus.FAILED),
        # A cancelled M-Pesa prompt comes back abandoned. Reporting it at once
        # is the whole point — otherwise the customer waits on nothing.
        ("abandoned", ChargeStatus.FAILED),
        # Genuinely still on the phone, unanswered: must NOT be settled.
        ("pending", ChargeStatus.PROCESSING),
    ],
)
def test_verify_settles_from_what_the_gateway_says(
    client, owner, monkeypatch, gateway_status, expected
):
    import asyncio

    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal

    async def run():
        async with AsyncSessionLocal() as db:
            sub_id = _sub_id(client, owner)
            charge = BillingCharge(
                subscription_id=sub_id,
                amount_cents=99900,
                currency="KES",
                status=ChargeStatus.PROCESSING,
                provider_reference=f"ref-{uuid.uuid4().hex[:8]}",
                idempotency_key=uuid.uuid4().hex,
                period_start=__import__("datetime").datetime.utcnow(),
                period_end=__import__("datetime").datetime.utcnow(),
            )
            db.add(charge)
            await db.commit()
            await db.refresh(charge)

            monkeypatch.setattr(paystack, "is_configured", lambda: True)

            async def fake_verify(ref):
                return {"status": gateway_status, "gateway_response": "test"}

            monkeypatch.setattr(paystack, "verify_transaction", fake_verify)

            settled = await billing.verify_charge_now(db, charge.id)
            return settled.status

    assert asyncio.get_event_loop().run_until_complete(run()) == expected


def test_verify_is_safe_to_poll(client, owner, monkeypatch):
    """The client polls every few seconds; a settled charge must not be applied
    twice, or one payment extends the period over and over."""
    import asyncio
    from datetime import datetime

    from app.core.database import AsyncSessionLocal

    async def run():
        async with AsyncSessionLocal() as db:
            charge = BillingCharge(
                subscription_id=_sub_id(client, owner),
                amount_cents=99900, currency="KES",
                status=ChargeStatus.PROCESSING,
                provider_reference=f"ref-{uuid.uuid4().hex[:8]}",
                idempotency_key=uuid.uuid4().hex,
                period_start=datetime.utcnow(), period_end=datetime.utcnow(),
            )
            db.add(charge)
            await db.commit()
            await db.refresh(charge)

            monkeypatch.setattr(paystack, "is_configured", lambda: True)

            async def fake_verify(ref):
                return {"status": "success", "gateway_response": "ok"}

            monkeypatch.setattr(paystack, "verify_transaction", fake_verify)

            from app.models import Subscription

            await billing.verify_charge_now(db, charge.id)
            sub = await db.get(Subscription, charge.subscription_id)
            end_1 = sub.current_period_end

            # Poll again, exactly as the browser does.
            await billing.verify_charge_now(db, charge.id)
            await db.refresh(sub)
            return end_1, sub.current_period_end

    a, b = asyncio.get_event_loop().run_until_complete(run())
    assert a == b, "polling extended the subscription a second time"
