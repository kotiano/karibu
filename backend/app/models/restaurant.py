"""Restaurant = tenant. Owns users, menu, orders, and one subscription."""
from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class Restaurant(BaseModel):
    __tablename__ = "restaurants"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    billing_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    users: Mapped[list["User"]] = relationship(back_populates="restaurant")
    subscription: Mapped["Subscription | None"] = relationship(
        back_populates="restaurant", uselist=False, cascade="all, delete-orphan"
    )
