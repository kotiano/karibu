"""Authentication, and the boundaries that must not move."""
import uuid


def _email():
    return f"u-{uuid.uuid4().hex[:10]}@example.com"


def test_register_then_login_is_blocked_until_confirmed(client):
    """Signup must not hand out a session. Email ownership is unproven until
    the link is clicked, so an unconfirmed account signing in would let anyone
    register with someone else's address and get in."""
    email = _email()
    r = client.post("/api/auth/register", json={
        "full_name": "A", "email": email, "password": "Str0ng!Passw0rd#2026",
        "restaurant_name": "R",
    })
    assert r.status_code == 201
    assert "tokens" not in (r.json().get("data") or {})

    r = client.post("/api/auth/login", json={"email": email, "password": "Str0ng!Passw0rd#2026"})
    assert r.status_code == 403


def test_verify_email_rejects_a_bad_token(client):
    r = client.post("/api/auth/verify-email", json={"token": "x" * 40})
    assert r.status_code == 400


def test_verify_email_signs_the_user_in(client, mail_log):
    """A confirmation link must return a session. Making the user click through
    and then type their password wastes the trust the click established."""
    from tests.conftest import register_and_confirm

    _, token, session = register_and_confirm(client, mail_log, restaurant="R2")
    assert session["tokens"]["access_token"]

    # Single use: the same link must not work twice.
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 400


def test_confirmation_link_is_single_use(client, mail_log):
    """Replaying a link must fail — a forwarded email should not re-open an
    account someone else has since taken over."""
    from tests.conftest import register_and_confirm

    _, token, _ = register_and_confirm(client, mail_log, restaurant="R3")
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 400


def test_wrong_password_is_rejected(client, owner):
    r = client.post("/api/auth/login", json={"email": owner["email"], "password": "wrong-password"})
    assert r.status_code == 401


def test_protected_route_needs_a_token(client):
    assert client.get("/api/orders").status_code == 403


def test_me_returns_the_signed_in_user(client, owner):
    r = client.get("/api/auth/me", headers=owner["headers"])
    assert r.status_code == 200
    assert r.json()["data"]["user"]["email"] == owner["email"]
