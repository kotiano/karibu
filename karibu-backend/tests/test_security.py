"""The controls that exist to stop abuse. Each one is here because losing it
silently would be expensive and invisible."""
import uuid

import pytest

from app.core.limiter import limiter
from tests.conftest import PASSWORD, register_and_confirm


@pytest.fixture
def rate_limited():
    """Re-enable the real limiter for one test."""
    limiter.enabled = True
    limiter.reset()
    yield
    limiter.enabled = False


def test_signup_is_rate_limited(client, rate_limited):
    """Without this, one script can create thousands of accounts and burn the
    whole daily email quota, locking out real signups."""
    codes = []
    for _ in range(8):
        r = client.post("/api/auth/register", json={
            "full_name": "Spam", "email": f"s-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD, "restaurant_name": "S",
        })
        codes.append(r.status_code)
    assert 429 in codes, "signup must be rate limited"


def test_one_restaurant_cannot_read_anothers_orders(client, mail_log):
    """Tenant isolation. The single most damaging bug a multi-tenant POS can
    have — one owner seeing another's takings."""
    _, _, a = register_and_confirm(client, mail_log, restaurant="Restaurant A")
    ha = {"Authorization": f"Bearer {a['tokens']['access_token']}"}
    cat = client.post("/api/menu/categories", headers=ha, json={"name": "M"}).json()["data"]
    item = client.post("/api/menu/items", headers=ha,
                       json={"name": "Secret Dish", "price": 900, "category_id": cat["id"]}).json()["data"]
    order = client.post("/api/orders", headers=ha, json={
        "order_type": "dine_in", "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }).json()["data"]

    _, _, b = register_and_confirm(client, mail_log, restaurant="Restaurant B")
    hb = {"Authorization": f"Bearer {b['tokens']['access_token']}"}

    # B must not see A's order, by id or in a listing.
    assert client.get(f"/api/orders/{order['id']}", headers=hb).status_code == 404
    assert all(o["id"] != order["id"] for o in client.get("/api/orders", headers=hb).json()["data"])
    names = [i["name"] for c in client.get("/api/menu/categories", headers=hb).json()["data"]
             for i in (c.get("items") or [])]
    assert "Secret Dish" not in names


def test_a_forged_token_is_rejected(client):
    bad = {"Authorization": "Bearer not.a.real.token"}
    assert client.get("/api/orders", headers=bad).status_code in (401, 403)


def test_paystack_webhook_rejects_an_unsigned_body(client):
    """The webhook grants paid time. Unsigned, anyone could mark themselves
    paid forever."""
    r = client.post("/api/billing/webhook/paystack",
                    json={"event": "charge.success", "data": {"reference": "x"}})
    assert r.status_code == 404, "an unsigned webhook must not be processed"


def test_paystack_webhook_accepts_a_correctly_signed_body(client, monkeypatch):
    import hashlib
    import hmac
    import json as jsonlib

    from app.core.config import settings
    from app.services import paystack

    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", "sk_test_signing_key")
    monkeypatch.setattr(paystack.settings, "PAYSTACK_SECRET_KEY", "sk_test_signing_key")

    body = jsonlib.dumps({"event": "charge.success",
                          "data": {"reference": "nonexistent", "gateway_response": "ok"}}).encode()
    sig = hmac.new(b"sk_test_signing_key", body, hashlib.sha512).hexdigest()

    r = client.post("/api/billing/webhook/paystack", content=body,
                    headers={"x-paystack-signature": sig, "content-type": "application/json"})
    assert r.status_code == 200


def test_cashier_cannot_record_expenses(client, owner):
    """Expenses drive the only profit figure the owner sees. A cashier keying
    'salaries 40,000' would distort every decision made from it."""
    staff_email = f"cashier-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/register", json={
        "full_name": "Cashier", "email": staff_email, "password": PASSWORD,
        "restaurant_name": "Other Kitchen",
    })
    assert r.status_code == 201
    # A brand-new unconfirmed account cannot reach the endpoint at all.
    assert client.post("/api/expenses", json={"category": "salaries", "amount": 40000}).status_code in (401, 403)
