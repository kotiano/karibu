"""Time is in East Africa Time, not UTC.

Two separate faults, so two separate groups of tests: how a timestamp is
SERIALISED (the browser read it three hours early) and where a DAY starts (the
dashboard reset at 3am local).
"""
from datetime import datetime, timedelta, timezone

from fastapi.encoders import jsonable_encoder

import app.main  # noqa: F401 — installs the datetime encoder
from app.core.timezone import day_bounds_utc, days_ago_utc, to_local, to_utc_iso


# ── Serialisation ───────────────────────────────────────────────────────────
def test_timestamps_carry_an_explicit_utc_marker():
    """Without the Z, `new Date()` in a browser reads it as LOCAL time."""
    out = jsonable_encoder({"created_at": datetime(2026, 8, 1, 5, 29, 22)})
    assert out["created_at"].endswith("Z"), out["created_at"]


def test_an_api_response_round_trips_to_the_right_wall_clock(client, owner):
    """08:13 in Nairobi must not display as 05:13."""
    order = client.post("/api/orders", json={
        "order_type": "takeaway",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": 1}],
    }, headers=owner["headers"]).json()["data"]

    stamp = order["created_at"]
    assert stamp.endswith("Z"), f"no timezone marker: {stamp}"
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    drift = abs((datetime.now(timezone.utc) - parsed).total_seconds())
    assert drift < 120, f"timestamp is {drift / 3600:.1f}h out"


# ── Day boundaries ──────────────────────────────────────────────────────────
def test_a_day_starts_at_local_midnight_not_utc_midnight():
    start, end = day_bounds_utc()
    # Nairobi is UTC+3, so local midnight is 21:00 UTC the day before.
    assert start.hour == 21, f"day starts at {start.hour}:00 UTC, expected 21:00"
    assert end - start == timedelta(days=1)


def test_a_sale_just_after_local_midnight_counts_as_today():
    """The actual bug: a 1am sale fell into the previous day's takings."""
    start, end = day_bounds_utc()
    one_am_local = start + timedelta(hours=1)   # 01:00 local
    assert start <= one_am_local < end
    assert to_local(one_am_local).hour == 1


def test_bucketing_uses_the_local_hour():
    """A 1pm sale belongs in the 1pm column, not 10am."""
    assert to_local(datetime(2026, 8, 1, 10, 0)).hour == 13


def test_days_ago_lands_on_a_local_midnight():
    assert days_ago_utc(1) == day_bounds_utc()[0] - timedelta(days=1)


def test_naive_values_are_treated_as_utc_not_local():
    assert to_utc_iso(datetime(2026, 8, 1, 5, 0)) == "2026-08-01T05:00:00Z"
