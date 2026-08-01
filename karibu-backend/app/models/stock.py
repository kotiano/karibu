"""Stock (inventory) — items on hand, and a ledger of every change.

TWO TABLES, NOT ONE. StockItem carries the current quantity so a list screen is
one query; StockMovement records every change that produced it. Keeping only the
running total would make "we are 4kg short" unanswerable — you could see the
shortfall and never who took it, when, or whether it was sold, wasted or
miscounted. Keeping only the ledger would mean summing every movement in history
to draw a list.

They are written in the SAME TRANSACTION, always, so they cannot disagree.

QUANTITIES ARE INTEGER THOUSANDTHS, never floats — the same discipline the money
columns use. 0.1kg + 0.2kg in binary floating point is not 0.3kg, and a stock
count that drifts by a hair per movement is worse than no stock count at all.
Three decimal places is enough for grams and millilitres.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class StockUnit:
    """Units a Kenyan kitchen actually buys in."""

    KG = "kg"
    GRAM = "g"
    LITRE = "l"
    ML = "ml"
    PIECE = "pc"
    CRATE = "crate"
    BOX = "box"
    PACKET = "packet"
    BOTTLE = "bottle"
    TRAY = "tray"

    ALL = (KG, GRAM, LITRE, ML, PIECE, CRATE, BOX, PACKET, BOTTLE, TRAY)


class MovementReason:
    """Why the quantity changed. The sign is implied but not enforced by it —
    a stock count can go either way."""

    RECEIVED = "received"   # a delivery came in
    USED = "used"           # consumed by the kitchen
    WASTE = "waste"         # spoiled, burnt, dropped
    COUNT = "count"         # physical count corrected the book figure
    RETURNED = "returned"   # sent back to the supplier

    ALL = (RECEIVED, USED, WASTE, COUNT, RETURNED)

    # The ones that should normally reduce stock. Used for reporting, not as a
    # constraint — a returned delivery is a decrease, a corrected undercount is
    # an increase, and forcing signs here would just make people lie to the form.
    OUTGOING = (USED, WASTE, RETURNED)


class StockItem(BaseModel):
    __tablename__ = "stock_items"

    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), default=StockUnit.KG, nullable=False)

    # Thousandths of a unit. 2.5kg is stored as 2500.
    quantity_milli: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Below this, the item shows as low. 0 disables the warning for items nobody
    # wants to be nagged about.
    reorder_level_milli: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # What one whole unit costs, so a stock level can be valued. Optional: plenty
    # of small kitchens track quantity and not cost.
    unit_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supplier: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Archived rather than deleted, always — movements reference this row, and
    # erasing an item would erase the history of what happened to it. Same
    # reasoning as archived menu items.
    is_archived: Mapped[bool] = mapped_column(default=False, nullable=False)

    movements: Mapped[list["StockMovement"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_stock_items_restaurant_archived", "restaurant_id", "is_archived"),
    )

    @property
    def quantity(self) -> float:
        return self.quantity_milli / 1000

    @property
    def reorder_level(self) -> float:
        return self.reorder_level_milli / 1000

    @property
    def is_low(self) -> bool:
        """A reorder level of 0 means 'never warn', not 'warn at zero'."""
        return self.reorder_level_milli > 0 and self.quantity_milli <= self.reorder_level_milli

    @property
    def value_cents(self) -> int | None:
        if self.unit_cost_cents is None:
            return None
        return round(self.quantity_milli * self.unit_cost_cents / 1000)


class StockMovement(BaseModel):
    __tablename__ = "stock_movements"

    stock_item_id: Mapped[str] = mapped_column(
        ForeignKey("stock_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item: Mapped["StockItem"] = relationship(back_populates="movements")

    # Signed. Positive adds to stock, negative removes — so replaying the ledger
    # reproduces the running total exactly.
    delta_milli: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The quantity AFTER this movement, captured at the time. Redundant with a
    # replay of the ledger, and worth it: it makes a history screen readable
    # without a running sum, and it exposes a divergence rather than hiding one.
    balance_after_milli: Mapped[int] = mapped_column(Integer, nullable=False)

    # What this delivery actually cost, when it was a purchase.
    #
    # Separate from StockItem.unit_cost_cents, which is the standing price used
    # to value the shelf. What a specific delivery cost is a different fact —
    # prices move, and overwriting the standing cost with the latest invoice
    # would silently restate the value of everything already on the shelf.
    total_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Who did it. Nullable only because a user can be deleted; the point of this
    # table is that a change has a name against it.
    recorded_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    recorded_by: Mapped["User | None"] = relationship()  # noqa: F821

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )

    @property
    def delta(self) -> float:
        return self.delta_milli / 1000

    @property
    def balance_after(self) -> float:
        return self.balance_after_milli / 1000
