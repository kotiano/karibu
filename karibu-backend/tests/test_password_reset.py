"""Forgot/reset password.

Same design rule as the rest of the suite: nothing touches the database. The
reset token is read out of the logged email exactly as a person reads it out of
their inbox, so a test can only pass through the path a real user walks.

Most of these assert a SECURITY property rather than a feature. The happy path
is one test; the other seven are the ways this endpoint is abused.
"""
import io
import re

from tests.conftest import PASSWORD, register_and_confirm

NEW_PASSWORD = "Fresh!Passw0rd#2026"


def request_reset(client, mail_log, email):
    """Ask for a reset and return the token from the emailed link."""
    mark = mail_log.tell()
    r = client.post("/api/auth/forgot-password", json={"email": email})
    assert r.status_code == 200, r.text
    mail_log.seek(mark)
    body = mail_log.read()
    mail_log.seek(0, io.SEEK_END)
    match = re.search(r"/reset-password\?token=([A-Za-z0-9_-]+)", body)
    return match.group(1) if match else None


def test_reset_password_end_to_end(client, mail_log):
    email, _, _ = register_and_confirm(client, mail_log)

    token = request_reset(client, mail_log, email)
    assert token, "no reset link was emailed"

    r = client.post("/api/auth/reset-password", json={"token": token, "password": NEW_PASSWORD})
    assert r.status_code == 200, r.text

    # The new password works...
    assert client.post("/api/auth/login", json={"email": email, "password": NEW_PASSWORD}).status_code == 200
    # ...and the old one does not.
    assert client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401


def test_reset_link_is_single_use(client, mail_log):
    email, _, _ = register_and_confirm(client, mail_log)
    token = request_reset(client, mail_log, email)

    assert client.post("/api/auth/reset-password", json={"token": token, "password": NEW_PASSWORD}).status_code == 200
    # Replaying the same link must fail, or an intercepted email stays live
    # forever.
    again = client.post("/api/auth/reset-password", json={"token": token, "password": "Another!Pass1"})
    assert again.status_code == 400


def test_reset_kills_existing_sessions(client, mail_log):
    """The point of a reset is often that somebody else is already inside."""
    email, _, session = register_and_confirm(client, mail_log)
    stolen = session["tokens"]["access_token"]
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {stolen}"}).status_code == 200

    token = request_reset(client, mail_log, email)
    client.post("/api/auth/reset-password", json={"token": token, "password": NEW_PASSWORD})

    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {stolen}"})
    assert r.status_code == 401, "a token issued before the reset still works"


def test_forgot_password_does_not_reveal_whether_an_email_exists(client, mail_log):
    email, _, _ = register_and_confirm(client, mail_log)

    known = client.post("/api/auth/forgot-password", json={"email": email})
    unknown = client.post("/api/auth/forgot-password", json={"email": "nobody-here@example.com"})

    assert known.status_code == unknown.status_code == 200
    assert known.json()["message"] == unknown.json()["message"]


def test_unknown_email_sends_no_link(client, mail_log):
    assert request_reset(client, mail_log, "definitely-not-registered@example.com") is None


def test_garbage_token_is_rejected(client):
    r = client.post("/api/auth/reset-password", json={
        "token": "z" * 40, "password": NEW_PASSWORD,
    })
    assert r.status_code == 400


def test_reset_enforces_the_password_rules(client, mail_log):
    """The rules the signup form draws as a checklist apply here too — a reset
    must not be a way around them."""
    email, _, _ = register_and_confirm(client, mail_log)
    token = request_reset(client, mail_log, email)

    weak = client.post("/api/auth/reset-password", json={"token": token, "password": "alllowercase"})
    assert weak.status_code == 422
    assert "password" in weak.json().get("errors", {})

    # And the token survives a rejected attempt, so a typo doesn't cost the
    # user their link.
    good = client.post("/api/auth/reset-password", json={"token": token, "password": NEW_PASSWORD})
    assert good.status_code == 200


def test_reset_confirms_an_unconfirmed_account(client, mail_log):
    """Someone who never received the signup email but did receive this one
    must not be left knowing their password and still unable to sign in."""
    import uuid

    email = f"unconfirmed-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/register", json={
        "full_name": "Never Confirmed", "email": email, "password": PASSWORD,
        "restaurant_name": "Limbo Cafe", "phone": "0712345678",
    })
    assert r.status_code == 201
    assert client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 403

    token = request_reset(client, mail_log, email)
    assert client.post("/api/auth/reset-password", json={"token": token, "password": NEW_PASSWORD}).status_code == 200
    assert client.post("/api/auth/login", json={"email": email, "password": NEW_PASSWORD}).status_code == 200
