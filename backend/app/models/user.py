"""User account model with secure password handling, roles, and tenant link."""
from __future__ import annotations

from datetime import datetime

from passlib.context import CryptContext
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

# bcrypt via passlib; deprecated schemes auto-upgrade on next login.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class UserRole:
    OWNER = "owner"
    MANAGER = "manager"
    CASHIER = "cashier"
    WAITER = "waiter"

    ALL = (OWNER, MANAGER, CASHIER, WAITER)


class User(BaseModel):
    __tablename__ = "users"

    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(
        String(120), unique=True, nullable=False, index=True
    )
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[str] = mapped_column(String(20), default=UserRole.CASHIER, nullable=False)
    branch_name: Mapped[str] = mapped_column(
        String(120), default="Main Branch", nullable=False
    )
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    restaurant_id: Mapped[str] = mapped_column(
        ForeignKey("restaurants.id"), nullable=False, index=True
    )
    restaurant: Mapped["Restaurant"] = relationship(back_populates="users")

    # Bumping this invalidates all previously issued tokens for the user.
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Account-level brute-force lockout. Per-IP rate limiting alone doesn't
    # stop an attacker rotating IPs against one account; these do.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Platform operator (SaaS admin). Sees across ALL tenants via the /admin
    # routes. NEVER settable through any API — only via create_admin.py.
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Email ownership confirmation. Login is blocked until confirmed.
    email_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    email_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    email_token_expires: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    orders: Mapped[list["Order"]] = relationship(back_populates="server")

    # --- Password helpers ----------------------------------------------------
    def set_password(self, raw_password: str) -> None:
        self.password_hash = pwd_context.hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return pwd_context.verify(raw_password, self.password_hash)
