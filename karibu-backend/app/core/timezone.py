"""Time handling.

TWO SEPARATE BUGS LIVE HERE, and they need different fixes.

1. THE API EMITTED NAKED TIMESTAMPS. Everything is stored as naive UTC, and
   FastAPI serialised it as "2026-08-01T05:29:22" with no offset. JavaScript
   parses a bare datetime as LOCAL time, so a UTC instant rendered three hours
   early in Nairobi — an order taken at 08:13 showed 05:13. Fixed by marking
   every outgoing datetime with Z, which is a statement of fact about data that
   was always UTC, not a conversion.

2. "TODAY" WAS A UTC DAY. Day boundaries came from datetime.utcnow(), so the
   dashboard's "sales today" reset at 3am Nairobi time and anything sold
   between midnight and 3am counted toward the previous day. A restaurant open
   past midnight would have closed its books on the wrong figures.

The display fix belongs in the client's locale; the boundary fix cannot, because
the SQL that sums a day has to know where the day starts.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings


def tz() -> ZoneInfo:
    """The restaurant's wall clock. Configurable, defaulting to Nairobi."""
    try:
        return ZoneInfo(settings.TIMEZONE)
    except Exception:  # noqa: BLE001 — a bad tz name must not take the app down
        return ZoneInfo("Africa/Nairobi")


def to_utc_iso(dt: datetime) -> str:
    """Serialise as UTC with an explicit Z. Naive values are ASSUMED UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def local_now() -> datetime:
    """Now, on the restaurant's wall clock."""
    return datetime.now(tz())


def day_bounds_utc(local_day: datetime | None = None) -> tuple[datetime, datetime]:
    """Midnight-to-midnight LOCAL, returned as naive UTC for querying.

    Naive on the way out because every timestamp column is naive UTC — handing
    SQLAlchemy an aware datetime against a naive column is the exact mismatch
    that produced the 500 on debts ("can't subtract offset-naive and
    offset-aware datetimes").
    """
    now_local = local_day.astimezone(tz()) if local_day else local_now()
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


def days_ago_utc(days: int) -> datetime:
    """Start of the local day `days` ago, as naive UTC."""
    start, _ = day_bounds_utc(local_now() - timedelta(days=days))
    return start


def to_local(dt: datetime) -> datetime:
    """A stored (naive UTC) timestamp on the restaurant's wall clock.

    Used wherever a timestamp is BUCKETED rather than displayed — by hour for
    the hourly chart, by date for the trend. Bucketing on the raw UTC value put
    a 1pm sale in the 10am column and a 1am sale on the previous day.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz())
