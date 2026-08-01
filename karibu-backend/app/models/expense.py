"""Restaurant running costs.

Deliberately separate from Payment. A Payment is money coming IN from a
customer; an Expense is money going OUT. Keeping them in one table with a sign
would make every analytics query a minefield — one forgotten filter and rent
gets counted as revenue.

Money is integer cents here as everywhere else in this app.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
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

    # INCURRED IS NOT PAID. A delivery taken on credit is a real cost the
    # moment it arrives and is not money out of the till until the supplier is
    # settled. Collapsing the two made "spent today" wrong in both directions:
    # it counted credit purchases as cash gone, and it showed nothing at all
    # when that supplier was finally paid weeks later.
    is_paid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # The stock delivery this cost came from, when it came from one.
    #
    # Buying stock is ONE event that used to need TWO manual entries — a stock
    # movement and an expense — with nothing tying them together. They drifted:
    # record the delivery and forget the expense and the books understate cost;
    # record the expense and forget the delivery and the shelf count is wrong.
    # Now the movement creates the expense, and this is the join.
    stock_movement_id: Mapped[str | None] = mapped_column(
        ForeignKey("stock_movements.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # The date the cost belongs to, which is NOT necessarily when it was keyed
    # in — an owner records Friday's purchases on Monday, and putting them on
    # Monday would misstate both days' figures.
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
