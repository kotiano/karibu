"""Paying when no billing number was given at signup.

The API always accepted a phone; the screen never asked for one, so it refused
the charge and told the user to go and find a setting.
"""
import uuid

from tests.conftest import PASSWORD


def signup_without_a_phone(client, mail_log):
    """Register with NO phone at all — the case that used to dead-end."""
    import io, re

    email = f"nophone-{uuid.uuid4().hex[:8]}@example.com"
    mark = mail_log.tell()
    r = client.post("/api/auth/register", json={
        "full_name": "No Phone", "email": email, "password": PASSWORD,
        "restaurant_name": "Phoneless Cafe",
    })
    assert r.status_code == 201, r.text
    mail_log.seek(mark)
    token = re.search(r"/verify\?token=([A-Za-z0-9_-]+)", mail_log.read()).group(1)
    mail_log.seek(0, io.SEEK_END)
    s = client.post("/api/auth/verify-email", json={"token": token}).json()["data"]
    return {"Authorization": f"Bearer {s['tokens']['access_token']}"}


def test_the_subscription_says_no_number_is_on_file(client, mail_log):
    """The screen cannot offer to fix this without being told."""
    h = signup_without_a_phone(client, mail_log)
    sub = client.get("/api/billing/subscription", headers=h).json()["data"]["subscription"]
    assert "billing_phone" in sub
    assert sub["billing_phone"] is None


def test_paying_with_no_number_asks_for_one(client, mail_log):
    h = signup_without_a_phone(client, mail_log)
    r = client.post("/api/billing/pay", json={}, headers=h)
    assert r.status_code == 422
    assert r.json()["errors"]["phone"] == "required"


def test_supplying_a_number_at_payment_saves_it(client, mail_log):
    """One go: type it, pay, and it is there next time."""
    h = signup_without_a_phone(client, mail_log)
    r = client.post("/api/billing/pay", json={"phone": "0712345678"}, headers=h)
    assert r.status_code == 200, r.text

    sub = client.get("/api/billing/subscription", headers=h).json()["data"]["subscription"]
    assert sub["billing_phone"] == "0712345678"


def test_a_bad_number_is_refused_before_any_charge(client, mail_log):
    """A malformed number produces a charge that never prompts anyone, which
    looks like the app is broken rather than like a typo."""
    h = signup_without_a_phone(client, mail_log)
    r = client.post("/api/billing/pay", json={"phone": "12345"}, headers=h)
    assert r.status_code == 422
    assert r.json()["errors"]["phone"] == "invalid"

    sub = client.get("/api/billing/subscription", headers=h).json()["data"]["subscription"]
    assert sub["billing_phone"] is None, "a rejected number must not be saved"


def test_paying_from_a_different_line_updates_the_saved_number(client, mail_log):
    h = signup_without_a_phone(client, mail_log)
    client.post("/api/billing/pay", json={"phone": "0712345678"}, headers=h)
    client.post("/api/billing/pay", json={"phone": "0798765432"}, headers=h)

    sub = client.get("/api/billing/subscription", headers=h).json()["data"]["subscription"]
    assert sub["billing_phone"] == "0798765432"


def test_accepted_formats(client, mail_log):
    for number in ("0712345678", "254712345678", "+254 712 345 678", "712345678"):
        h = signup_without_a_phone(client, mail_log)
        r = client.post("/api/billing/pay", json={"phone": number}, headers=h)
        assert r.status_code == 200, f"{number} was refused: {r.text}"
