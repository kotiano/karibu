"""User account model with secure password handling, roles, and tenant link."""
from __future__ import annotations

from datetime import datetime

from passlib.context import CryptContext
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

# bcrypt via passlib; deprecated schemes auto-upgrade on next login.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class UserRole:
    """Three ranks. Whoever signs the restaurant up is the manager.

    There is no separate "owner" above manager. In a restaurant this size they
    are the same person, and a rank that only ever has one holder buys nothing
    but a permission matrix with an unused row.

    RANK ORDER IS LOAD-BEARING — `rank()` is what stops a manager minting
    another manager, or a cashier promoting themselves.
    """

    MANAGER = "manager"
    CASHIER = "cashier"
    WAITER = "waiter"

    ALL = (MANAGER, CASHIER, WAITER)

    # Everyone who may see money-wide screens and manage staff.
    MANAGERS = (MANAGER,)
    #
    # There is deliberately NO "handles money" rank. Recording a payment is open
    # to every role — a waiter handed cash has to be able to write it down, and
    # blocking them does not stop the money moving, only its being recorded.
    # What holds people to it is the accountability ledger: an unrecorded
    # payment leaves the order unpaid against the person who served it.
    #
    # Voiding an order is the opposite case and IS manager-only, because that
    # erases a record rather than creating one.

    _RANK = {MANAGER: 3, CASHIER: 2, WAITER: 1}

    @classmethod
    def rank(cls, role: str) -> int:
        return cls._RANK.get(role, 0)


class User(BaseModel):
    __tablename__ = "users"

    full_name: Mapped[str] = mapped_column(String(120), nullable=False)

    # NULLABLE, and unique only where present. A waiter may genuinely not have
    # an email address, and requiring one produces a database full of invented
    # ones. Uniqueness is enforced by a partial index in the migration rather
    # than a column constraint, because "unique among the rows that have one"
    # is not expressible here.
    email: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    # The other login identifier, and the one most staff will actually use.
    # Unique PER RESTAURANT, not globally: the same person may cook here in the
    # evening and somewhere else at lunch, and a global constraint would make
    # the second account impossible.
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)

    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[str] = mapped_column(String(20), default=UserRole.WAITER, nullable=False)

    # Set when a manager creates the account with a temporary code. While true
    # the session can reach exactly one endpoint — change-password — so the
    # manager who issued the code cannot go on acting as this person. That is
    # what makes "served by Jane" mean anything.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    invited_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
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
    # sha256 hash of the active email-confirmation token (never the token
    # itself, so a database leak yields no working link) plus its expiry.
    # sha256 hex is exactly 64 characters, which is why this column is
    # String(64).
    #
    # `attempts` is a leftover from the 6-digit-code era and is no longer
    # incremented: a 256-bit token cannot be brute-forced, so there is nothing
    # for an attempt cap to protect. Kept rather than migrated away because
    # dropping a column earns nothing here.
    email_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    email_token_expires: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    email_token_attempts: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    # Password reset. A SEPARATE pair from the email_token columns above, not a
    # reuse of them: the two links grant different things, and sharing one slot
    # would mean requesting a reset silently invalidates a pending signup
    # confirmation (and the reverse). Same sha256-at-rest treatment — a database
    # leak must not hand over a working reset link.
    reset_token: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    reset_token_expires: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    orders: Mapped[list["Order"]] = relationship(back_populates="server")

    __table_args__ = (
        # One phone per restaurant. The same number CAN appear under a
        # different restaurant — see the comment on `phone`.
        UniqueConstraint("restaurant_id", "phone", name="uq_users_restaurant_phone"),
    )

    @property
    def login_identifier(self) -> str:
        """What this person types to sign in. Email where they have one."""
        return self.email or self.phone or ""

    # --- Password helpers ----------------------------------------------------
    def set_password(self, raw_password: str) -> None:
        self.password_hash = pwd_context.hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return pwd_context.verify(raw_password, self.password_hash)
