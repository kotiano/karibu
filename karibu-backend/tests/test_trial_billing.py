"""Paying during a trial must not cost the days already promised."""
from datetime import datetime, timedelta

from app.core.config import settings
from app.models import Subscription, SubscriptionStatus
from app.services.billing import _period_for


def test_early_payment_starts_after_the_trial_ends():
    now = datetime.utcnow()
    sub = Subscription(
        status=SubscriptionStatus.TRIALING,
        trial_ends_at=now + timedelta(days=5),   # 5 days still owed
        current_period_end=None,
    )
    start, end = _period_for(sub, now)
    assert start == sub.trial_ends_at, "the paid month must not swallow the trial"
    assert end == sub.trial_ends_at + timedelta(days=settings.BILLING_PERIOD_DAYS)


def test_payment_after_the_trial_starts_now():
    now = datetime.utcnow()
    sub = Subscription(
        status=SubscriptionStatus.TRIALING,
        trial_ends_at=now - timedelta(days=1),   # already over
        current_period_end=None,
    )
    start, _ = _period_for(sub, now)
    assert start == now


def test_renewal_stacks_on_the_current_period():
    now = datetime.utcnow()
    end = now + timedelta(days=3)
    sub = Subscription(status=SubscriptionStatus.ACTIVE,
                       trial_ends_at=now - timedelta(days=40),
                       current_period_end=end)
    start, _ = _period_for(sub, now)
    assert start == end, "a renewal must extend, not restart"


def test_a_long_lapsed_period_is_not_back_dated():
    now = datetime.utcnow()
    sub = Subscription(status=SubscriptionStatus.ACTIVE,
                       trial_ends_at=None,
                       current_period_end=now - timedelta(days=120))
    start, _ = _period_for(sub, now)
    assert start == now, "nobody should be sold a month that already passed"
