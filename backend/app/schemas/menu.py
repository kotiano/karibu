"""Menu schemas."""
from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


# --- Requests ---------------------------------------------------------------
class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    icon: str = "restaurant"
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    icon: str | None = None
    sort_order: int | None = None


class MenuItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    price: float = Field(ge=0)
    category_id: str
    description: str | None = None
    image_url: str | None = None
    prep_minutes: int = 15
    is_available: bool = True


class MenuItemUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    price: float | None = Field(default=None, ge=0)
    category_id: str | None = None
    description: str | None = None
    image_url: str | None = None
    prep_minutes: int | None = None
    is_available: bool | None = None


# --- Responses --------------------------------------------------------------
class MenuItemOut(ORMModel):
    id: str
    name: str
    description: str | None = None
    price: float
    price_cents: int
    image_url: str | None = None
    is_available: bool
    prep_minutes: int
    category_id: str


class CategoryOut(ORMModel):
    id: str
    name: str
    icon: str
    sort_order: int


class CategoryWithItems(CategoryOut):
    items: list[MenuItemOut] = []
