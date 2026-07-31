"""Auth + user schemas. Request models get validation for free (a core FastAPI
benefit over the hand-rolled Flask checks)."""
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel


# --- Requests ---------------------------------------------------------------
class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    restaurant_name: str = Field(min_length=1, max_length=160)
    phone: str | None = None
    billing_phone: str | None = None


class LoginRequest(BaseModel):
    """Sign-in identifier: an email address or a phone number.

    NOT EmailStr. Staff created by a manager may have only a phone, and
    validating this as an email would reject them before the handler ever ran.
    `email` is accepted as an alias so existing clients keep working.
    """

    identifier: str = Field(min_length=3, max_length=120, alias="email")
    password: str

    model_config = {"populate_by_name": True}


class VerifyEmailRequest(BaseModel):
    """The token from a confirmation link.

    No email field: the token alone identifies the account, so asking for an
    email as well would let someone probe which addresses are registered.
    """
    token: str = Field(min_length=20, max_length=200)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """The token from a reset link, plus the new password.

    No email field, for the same reason as VerifyEmailRequest: the token alone
    identifies the account, and asking for an address as well would turn this
    into an oracle for which addresses are registered.
    """
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class UpdateProfileRequest(BaseModel):
    full_name: str | None = Field(default=None, max_length=120)
    phone: str | None = None
    avatar_url: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    current_password: str | None = None


# --- Responses --------------------------------------------------------------
class UserOut(ORMModel):
    id: str
    full_name: str
    # Nullable: staff created by a manager may sign in by phone instead.
    email: str | None = None
    phone: str | None = None
    role: str
    avatar_url: str | None = None
    must_change_password: bool = False
    is_active: bool
    is_platform_admin: bool = False
    restaurant_id: str
    created_at: datetime


class RestaurantOut(ORMModel):
    id: str
    name: str
    billing_phone: str | None = None
    is_active: bool
    created_at: datetime


class Tokens(BaseModel):
    access_token: str
    refresh_token: str
