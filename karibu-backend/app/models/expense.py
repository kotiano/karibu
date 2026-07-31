"""Restaurant running costs.

Deliberately separate from Payment. A Payment is money coming IN from a
customer; an Expense is money going OUT. Keeping them in one table with a sign
would make every analytics query a minefield — one forgotten filter and rent
gets counted as revenue.

Money is integer cents here as everywhere else in this app.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class ExpenseCategory:
    """Fixed list rather than free text.

    Free-typed categories become "Rent", "rent", "RENT " within a week, and the
    breakdown that makes the feature useful stops adding up. Owners pick from
    these; the note field carries the specifics.
    """

    STOCK = "stock"            # ingredients, drinks, supplies
    RENT = "rent"
    SALARIES = "salaries"
    UTILITIES = "utilities"    # power, water, internet
    TRANSPORT = "transport"
    EQUIPMENT = "equipment"
    LICENCES = "licences"      # county permits, health certificates
    MARKETING = "marketing"
    REPAIRS = "repairs"
    OTHER = "other"

    ALL = (
        STOCK, RENT, SALARIES, UTILITIES, TRANSPORT,
        EQUIPMENT, LICENCES, MARKETING, REPAIRS, OTHER,
    )


class Expense(BaseModel):
    __tablename__ = "expenses"

    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id"), nullable=False, index=True
    )

    category: Mapped[str] = mapped_column(
        String(20), default=ExpenseCategory.OTHER, nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Who or what it was paid to — "Mama Mboga", "KPLC", "Landlord".
    payee: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # cash / mpesa / card / bank — how it left the business.
    method: Mapped[str] = mapped_column(String(20), default="cash", nullable=False)
    reference: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # The date the money actually left, which is NOT necessarily when it was
    # keyed in — an owner records Friday's payments on Monday, and putting them
    # on Monday would misstate both days' figures.
    spent_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )

    recorded_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    recorded_by: Mapped["User | None"] = relationship()

    __table_args__ = (
        # Every read is "this restaurant, this date range".
        Index("ix_expenses_restaurant_spent", "restaurant_id", "spent_at"),
    )

    @property
    def amount(self) -> float:
        return round(self.amount_cents / 100, 2)
