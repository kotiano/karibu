"""Stock request schemas.

Quantities arrive as decimals because that is what a person types ("2.5"), and
are converted to integer thousandths at this boundary. Nothing downstream sees
a float.
"""
from pydantic import BaseModel, Field, field_validator

from app.models import MovementReason, StockUnit


def to_milli(value: float) -> int:
    """Decimal units -> integer thousandths, rounded once, here."""
    return round(value * 1000)


class StockItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    unit: str = Field(default=StockUnit.KG)
    quantity: float = Field(default=0, ge=0, le=1_000_000)
    reorder_level: float = Field(default=0, ge=0, le=1_000_000)
    unit_cost: float | None = Field(default=None, ge=0, le=10_000_000)
    supplier: str | None = Field(default=None, max_length=120)
    note: str | None = None

    @field_validator("unit")
    @classmethod
    def known_unit(cls, v: str) -> str:
        if v not in StockUnit.ALL:
            raise ValueError(f"Unit must be one of: {', '.join(StockUnit.ALL)}")
        return v


class StockItemUpdate(BaseModel):
    """Quantity is NOT here on purpose.

    Stock levels change only through a movement, so that every change has a
    reason and a name against it. Letting the edit form set the number directly
    would give anyone a silent way to make a shortfall disappear, which is the
    one thing this table exists to prevent. A miscount is corrected with a
    `count` movement.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    unit: str | None = None
    reorder_level: float | None = Field(default=None, ge=0, le=1_000_000)
    unit_cost: float | None = Field(default=None, ge=0, le=10_000_000)
    supplier: str | None = Field(default=None, max_length=120)
    note: str | None = None

    @field_validator("unit")
    @classmethod
    def known_unit(cls, v: str | None) -> str | None:
        if v is not None and v not in StockUnit.ALL:
            raise ValueError(f"Unit must be one of: {', '.join(StockUnit.ALL)}")
        return v


class MovementCreate(BaseModel):
    reason: str
    # Signed. A delivery is positive, usage is negative — the client sends what
    # it means rather than relying on the reason to imply a direction, because
    # `count` legitimately goes either way.
    quantity: float = Field(ge=-1_000_000, le=1_000_000)
    note: str | None = None

    # A delivery's cost. Given here rather than keyed separately as an expense,
    # because buying stock is ONE event — see the comment on
    # Expense.stock_movement_id.
    cost: float | None = Field(default=None, ge=0, le=100_000_000)
    # False = taken on credit. The cost is real immediately; the money is not
    # out of the till until the supplier is settled.
    paid: bool = True
    supplier: str | None = Field(default=None, max_length=120)

    @field_validator("reason")
    @classmethod
    def known_reason(cls, v: str) -> str:
        if v not in MovementReason.ALL:
            raise ValueError(f"Reason must be one of: {', '.join(MovementReason.ALL)}")
        return v

    @field_validator("quantity")
    @classmethod
    def not_zero(cls, v: float) -> float:
        if to_milli(v) == 0:
            raise ValueError("Quantity must not be zero")
        return v
