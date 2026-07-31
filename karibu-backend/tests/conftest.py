"""Shared fixtures.

DESIGN RULE: tests never touch the database directly. Everything goes through
the HTTP API, including account confirmation — the token is read out of the
logged confirmation email, which is exactly what a real user does with their
inbox. That keeps the tests honest (they exercise the real paths) and avoids
the trap that broke the first version: fixtures opening their own event loops
left the shared engine holding connections bound to a closed loop, which failed
several tests in, looking like pollution rather than the loop mismatch it was.
"""
import io
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

import pytest

# Must be set BEFORE app.core.config is imported — Settings reads the
# environment at import time and is cached.
_TMP = Path(tempfile.mkdtemp(prefix="karibu-tests-"))
os.environ.update(
    DATABASE_URL=f"sqlite+aiosqlite:///{_TMP}/test.db",
    ENV="development",
    EMAIL_PROVIDER="smtp",
    SMTP_HOST="",              # console fallback — no mail leaves the machine
    PAYSTACK_SECRET_KEY="",    # charges simulate; no network call
    ENABLE_SCHEDULER="false",  # the billing sweep must not fire mid-test
    PUBLIC_WEB_URL="http://localhost:3000",
)

from fastapi.testclient import TestClient  # noqa: E402

# ORDER MATTERS. `import app.models` binds the name `app` to the PACKAGE, so
# doing it after `from app.main import app` replaces the FastAPI instance with a
# module — and TestClient then fails with "'module' object is not callable".
import app.models  # noqa: E402,F401  — registers every table on the metadata
from app.core.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

from app.core.limiter import limiter  # noqa: E402

PASSWORD = "Str0ng!Passw0rd#2026"

# Every test shares one client IP, so the real 5/minute signup limit starts
# refusing fixtures a few tests in — the limiter working correctly, but not
# what any of these tests are about. Disabled globally and re-enabled only in
# the test that exists to prove it still works (see test_security.py).
limiter.enabled = False


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Create the schema once, on its own loop, before the app ever starts."""
    import asyncio

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()   # safe here: the app has not connected yet

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(create())
    finally:
        loop.close()
    yield


@pytest.fixture(scope="session")
def client():
    """One TestClient for the session, so the app's engine keeps one loop."""
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def mail_log():
    """Captures the console-fallback emails so tests can read the link out."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logger = logging.getLogger("karibu.email")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield buf
    logger.removeHandler(handler)


def register_and_confirm(client, mail_log, *, restaurant="Test Kitchen"):
    """Sign a new owner up the way a person does: register, then click the link."""
    email = f"owner-{uuid.uuid4().hex[:10]}@example.com"
    mark = mail_log.tell()

    r = client.post("/api/auth/register", json={
        "full_name": "Test Owner", "email": email, "password": PASSWORD,
        "restaurant_name": restaurant, "phone": "0712345678",
    })
    assert r.status_code == 201, r.text

    mail_log.seek(mark)
    token = re.search(r"/verify\?token=([A-Za-z0-9_-]+)", mail_log.read()).group(1)
    mail_log.seek(0, io.SEEK_END)

    session = client.post("/api/auth/verify-email", json={"token": token}).json()["data"]
    return email, token, session


@pytest.fixture
def owner(client, mail_log):
    """A confirmed owner on a trial (which grants access) with one menu item."""
    email, _, session = register_and_confirm(client, mail_log)
    headers = {"Authorization": f"Bearer {session['tokens']['access_token']}"}

    cat = client.post("/api/menu/categories", headers=headers,
                      json={"name": "Mains"}).json()["data"]
    item = client.post("/api/menu/items", headers=headers, json={
        "name": "Nyama Choma", "price": 500, "category_id": cat["id"],
    }).json()["data"]

    return {"email": email, "password": PASSWORD, "headers": headers,
            "category": cat, "item": item, "session": session}
