"""Customer debt (credit) tracking.

A debt is deliberately NOT a Payment. When a customer takes an order on credit,
we record a Debt — a promise to pay — which lets the kitchen fulfil the order
without the amount ever counting as revenue. Revenue is the sum of Payment
rows, and a Debt is not one; so unpaid credit can never inflate sales.

When the customer settles (fully or partially), we create a real Payment dated
at that moment and reduce the debt. That Payment is what shows up in analytics,
on the payoff date — cash-basis accounting, which is what small restaurants
actually run on.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class DebtStatus:
    OUTSTANDING = "outstanding"  # still owed (fully or partially)
    SETTLED = "settled"          # fully paid off


class Debt(BaseModel):
    __tablename__ = "debts"

    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id"), nullable=False, index=True
    )
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id"), nullable=False, index=True
    )
    order: Mapped["Order"] = relationship()

    # Who owes, and the contact so staff can follow up.
    customer_name: Mapped[str] = mapped_column(String(120), nullable=False)
    customer_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # Original amount taken on credit, and how much has since been paid back.
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default=DebtStatus.OUTSTANDING, nullable=False, index=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at_idx: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_debts_restaurant_status", "restaurant_id", "status"),
    )

    @property
    def outstanding_cents(self) -> int:
        return max(self.amount_cents - self.paid_cents, 0)

    @property
    def is_overdue(self) -> bool:
        return (
            self.status == DebtStatus.OUTSTANDING
            and self.due_date is not None
            and datetime.utcnow() > self.due_date
        )
