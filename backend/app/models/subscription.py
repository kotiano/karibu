"""Subscription + billing models.

The correctness guarantees against double-billing are identical to the Flask
version and live at the SQLAlchemy/DB level, so they port unchanged:

1. One OPEN charge per period — a partial unique index the DB enforces.
2. Idempotency key per attempt.
3. Callback idempotency via unique CheckoutRequestID + ProcessedCallback ledger.
4. Terminal charge states that later callbacks can't reopen.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class SubscriptionStatus:
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"

    ACCESS_OK = (TRIALING, ACTIVE, PAST_DUE)


class ChargeStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"

    TERMINAL = (SUCCESS, FAILED)
    OPEN = (PENDING, PROCESSING)


class Subscription(BaseModel):
    __tablename__ = "subscriptions"

    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id"), unique=True, nullable=False
    )
    restaurant: Mapped["Restaurant"] = relationship(back_populates="subscription")

    status: Mapped[str] = mapped_column(
        String(20), default=SubscriptionStatus.TRIALING, nullable=False
    )
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KES", nullable=False)

    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    charges: Mapped[list["BillingCharge"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan", lazy="selectin"
    )

    # --- Derived helpers -----------------------------------------------------
    @property
    def has_access(self) -> bool:
        if self.status not in SubscriptionStatus.ACCESS_OK:
            return False
        if self.status == SubscriptionStatus.TRIALING:
            return self.trial_ends_at is None or datetime.utcnow() < self.trial_ends_at
        return True

    @property
    def in_trial(self) -> bool:
        return (
            self.status == SubscriptionStatus.TRIALING
            and self.trial_ends_at is not None
            and datetime.utcnow() < self.trial_ends_at
        )

    def is_renewal_due(self, now: datetime | None = None) -> bool:
        now = now or datetime.utcnow()
        if self.status == SubscriptionStatus.TRIALING:
            return self.trial_ends_at is not None and now >= self.trial_ends_at
        if self.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE):
            return self.current_period_end is not None and now >= self.current_period_end
        return False


class BillingCharge(BaseModel):
    __tablename__ = "billing_charges"

    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=False, index=True
    )
    subscription: Mapped["Subscription"] = relationship(back_populates="charges")

    status: Mapped[str] = mapped_column(
        String(20), default=ChargeStatus.PENDING, nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KES", nullable=False)

    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    idempotency_key: Mapped[str] = mapped_column(
        String(80), unique=True, nullable=False, index=True
    )

    mpesa_checkout_id: Mapped[str | None] = mapped_column(
        String(80), unique=True, nullable=True, index=True
    )
    mpesa_merchant_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    mpesa_receipt: Mapped[str | None] = mapped_column(String(40), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)

    result_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_desc: Mapped[str | None] = mapped_column(String(255), nullable=True)

    requested_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __table_args__ = (
        # At most ONE open (pending/processing) charge per subscription+period.
        # DB-enforced — the hard stop against double billing under concurrency.
        Index(
            "uq_open_charge_per_period",
            "subscription_id",
            "period_start",
            unique=True,
            sqlite_where=text("status IN ('pending','processing')"),
            postgresql_where=text("status IN ('pending','processing')"),
        ),
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in ChargeStatus.TERMINAL

    def mark_success(self, receipt: str | None, code: int, desc: str) -> None:
        self.status = ChargeStatus.SUCCESS
        self.mpesa_receipt = receipt
        self.result_code = code
        self.result_desc = desc
        self.finalized_at = datetime.utcnow()

    def mark_failed(self, code: int | None, desc: str) -> None:
        self.status = ChargeStatus.FAILED
        self.result_code = code
        self.result_desc = desc
        self.finalized_at = datetime.utcnow()


class ProcessedCallback(BaseModel):
    """Ledger of accepted M-Pesa callbacks, keyed by CheckoutRequestID, for O(1)
    duplicate detection and raw audit."""

    __tablename__ = "processed_callbacks"

    checkout_id: Mapped[str] = mapped_column(
        String(80), unique=True, nullable=False, index=True
    )
    result_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
