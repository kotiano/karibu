"""Staff request schemas."""
from pydantic import BaseModel, EmailStr, Field


class StaffCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    role: str
    # Both optional individually; the handler requires at least one, because
    # "you must give me a way for this person to sign in" is a rule about the
    # pair rather than about either field.
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=20)


class StaffUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    role: str | None = None
    is_active: bool | None = None
