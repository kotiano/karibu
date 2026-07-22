"""Append-only audit log.

Records significant actions ("who did what, when, from where") for forensics
and operator review. Deliberately write-once: rows are inserted, never updated
or deleted by application code, so the trail can't be quietly rewritten.

Kept denormalized (actor email, restaurant name captured at write time) so an
entry stays readable even if the referenced user or restaurant is later
renamed or removed.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class AuditAction:
    # Auth / account
    USER_REGISTERED = "user.registered"
    USER_LOGIN = "user.login"
    EMAIL_CONFIRMED = "user.email_confirmed"
    PASSWORD_CHANGED = "user.password_changed"
    STAFF_CREATED = "user.staff_created"
    STAFF_DEACTIVATED = "user.staff_deactivated"

    # Billing
    CHARGE_SUCCEEDED = "billing.charge_succeeded"
    CHARGE_FAILED = "billing.charge_failed"
    SUBSCRIPTION_SUSPENDED = "billing.subscription_suspended"

    # Admin (operator) actions
    ADMIN_TRIAL_EXTENDED = "admin.trial_extended"
    ADMIN_MONTH_COMPED = "admin.month_comped"
    ADMIN_SUSPENDED = "admin.suspended"
    ADMIN_REACTIVATED = "admin.reactivated"


class AuditLog(BaseModel):
    __tablename__ = "audit_logs"

    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)

    # Who did it (nullable: some events are system-driven, e.g. a dunning fail).
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # What tenant it concerns (nullable for platform-level events).
    restaurant_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    restaurant_name: Mapped[str | None] = mapped_column(String(160), nullable=True)

    # Optional target (e.g. the staff member acted upon).
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Human-readable summary + structured detail (JSON-encoded string).
    summary: Mapped[str] = mapped_column(String(400), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Where from.
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)

    created_at_idx: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_audit_restaurant_time", "restaurant_id", "created_at_idx"),
        Index("ix_audit_action_time", "action", "created_at_idx"),
    )
