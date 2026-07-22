"""Menu domain: categories and individual food/drink items."""
from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class Category(BaseModel):
    __tablename__ = "categories"

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    icon: Mapped[str] = mapped_column(String(40), default="restaurant", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id"), nullable=False, index=True
    )

    items: Mapped[list["MenuItem"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class MenuItem(BaseModel):
    __tablename__ = "menu_items"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Money stored in integer cents to avoid float rounding.
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    prep_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)

    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"), nullable=False)
    category: Mapped["Category"] = relationship(back_populates="items")

    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id"), nullable=False, index=True
    )

    @property
    def price(self) -> float:
        return round(self.price_cents / 100, 2)
