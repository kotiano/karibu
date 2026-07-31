"""Expense request schemas."""
from pydantic import BaseModel, Field

from app.schemas.order import NaiveUTCDatetime


class ExpenseCreate(BaseModel):
    category: str
    amount: float = Field(gt=0)
    note: str | None = None
    payee: str | None = Field(default=None, max_length=120)
    method: str = "cash"
    reference: str | None = Field(default=None, max_length=120)
    # Browsers send ISO-8601 with a Z; NaiveUTCDatetime normalises it to the
    # naive UTC every datetime column here stores. Without it Postgres rejects
    # the insert outright.
    spent_at: NaiveUTCDatetime | None = None


class ExpenseUpdate(BaseModel):
    category: str | None = None
    amount: float | None = Field(default=None, gt=0)
    note: str | None = None
    payee: str | None = Field(default=None, max_length=120)
    method: str | None = None
    reference: str | None = Field(default=None, max_length=120)
    spent_at: NaiveUTCDatetime | None = None
