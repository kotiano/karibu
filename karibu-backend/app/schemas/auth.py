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
    branch_name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class VerifyEmailRequest(BaseModel):
    """The token from a confirmation link.

    No email field: the token alone identifies the account, so asking for an
    email as well would let someone probe which addresses are registered.
    """
    token: str = Field(min_length=20, max_length=200)


class UpdateProfileRequest(BaseModel):
    full_name: str | None = Field(default=None, max_length=120)
    phone: str | None = None
    avatar_url: str | None = None
    branch_name: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    current_password: str | None = None


# --- Responses --------------------------------------------------------------
class UserOut(ORMModel):
    id: str
    full_name: str
    email: str
    phone: str | None = None
    role: str
    branch_name: str
    avatar_url: str | None = None
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
