"""Order domain: orders, their line items, and payments."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class OrderStatus:
    PENDING = "pending"
    PREPARING = "preparing"
    READY = "ready"
    SERVED = "served"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

    ALL = (PENDING, PREPARING, READY, SERVED, COMPLETED, CANCELLED)
    OPEN = (PENDING, PREPARING, READY, SERVED)


class OrderType:
    DINE_IN = "dine_in"
    TAKEAWAY = "takeaway"
    DELIVERY = "delivery"


class PaymentMethod:
    CASH = "cash"
    MPESA = "mpesa"
    CARD = "card"
    DEBT = "debt"  # taken on credit — recorded as a Debt, not a Payment


class PaymentStatus:
    UNPAID = "unpaid"
    PARTIAL = "partial"
    PAID = "paid"


class Order(BaseModel):
    __tablename__ = "orders"

    reference: Mapped[str] = mapped_column(
        String(20), unique=True, nullable=False, index=True
    )

    order_type: Mapped[str] = mapped_column(
        String(20), default=OrderType.DINE_IN, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default=OrderStatus.PENDING, nullable=False
    )
    payment_status: Mapped[str] = mapped_column(
        String(20), default=PaymentStatus.UNPAID, nullable=False
    )

    table_number: Mapped[str | None] = mapped_column(String(10), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    server_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    server: Mapped["User | None"] = relationship(back_populates="orders")

    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id"), nullable=False, index=True
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        # Every list/report query filters by restaurant + (recency | status).
        # Composite indexes turn those from tenant-wide scans into index seeks.
        Index("ix_orders_restaurant_created", "restaurant_id", "created_at"),
        Index("ix_orders_restaurant_status", "restaurant_id", "status"),
    )

    # VAT removed — prices are all-inclusive; no tax added on top.
    TAX_RATE = 0.0

    # --- Money helpers -------------------------------------------------------
    def recalculate(self) -> "Order":
        # Column defaults aren't applied until flush, so coerce None → 0 for
        # values that may be read before the row is flushed.
        self.discount_cents = self.discount_cents or 0
        self.subtotal_cents = sum(li.line_total_cents for li in self.items)
        self.tax_cents = 0  # no VAT
        self.total_cents = max(self.subtotal_cents - self.discount_cents, 0)
        return self

    @property
    def amount_paid_cents(self) -> int:
        return sum(p.amount_cents for p in self.payments)

    @property
    def balance_cents(self) -> int:
        return max(self.total_cents - self.amount_paid_cents, 0)

    def sync_payment_status(self) -> "Order":
        paid = self.amount_paid_cents
        if paid <= 0:
            self.payment_status = PaymentStatus.UNPAID
        elif paid < self.total_cents:
            self.payment_status = PaymentStatus.PARTIAL
        else:
            self.payment_status = PaymentStatus.PAID
        return self


class OrderItem(BaseModel):
    __tablename__ = "order_items"

    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id"), nullable=False, index=True
    )
    order: Mapped["Order"] = relationship(back_populates="items")

    menu_item_id: Mapped[str] = mapped_column(
        ForeignKey("menu_items.id"), nullable=False
    )

    # Snapshot name/price so historical orders stay correct if the menu changes.
    name_snapshot: Mapped[str] = mapped_column(String(120), nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    modifiers: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def line_total_cents(self) -> int:
        return self.unit_price_cents * self.quantity


class Payment(BaseModel):
    __tablename__ = "payments"

    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id"), nullable=False, index=True
    )
    order: Mapped["Order"] = relationship(back_populates="payments")

    method: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(60), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )
