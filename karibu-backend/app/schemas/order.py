"""Order + billing schemas."""
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


# --- Order requests ---------------------------------------------------------
class OrderLineIn(BaseModel):
    menu_item_id: str
    quantity: int = Field(default=1, ge=1)
    modifiers: str | None = None


class OrderCreate(BaseModel):
    order_type: str = "dine_in"
    table_number: str | None = None
    customer_name: str | None = None
    notes: str | None = None
    discount: float = Field(default=0, ge=0)
    items: list[OrderLineIn] = Field(min_length=1)


class StatusUpdate(BaseModel):
    status: str


class OrderPaymentIn(BaseModel):
    method: str
    amount: float = Field(gt=0)
    reference: str | None = None
    # Required only when method == "debt": who owes and when it's due.
    customer_name: str | None = None
    customer_phone: str | None = None
    due_date: datetime | None = None


class DebtPaymentIn(BaseModel):
    """A repayment against an outstanding debt."""
    amount: float = Field(gt=0)
    method: str = "cash"  # how they're settling (cash/mpesa/card)
    reference: str | None = None


# --- Order responses --------------------------------------------------------
class OrderItemOut(BaseModel):
    id: str
    menu_item_id: str
    name: str
    unit_price: float
    quantity: int
    modifiers: str | None = None
    line_total: float


class PaymentOut(BaseModel):
    id: str
    method: str
    amount: float
    reference: str | None = None
    received_at: datetime


class OrderOut(BaseModel):
    id: str
    reference: str
    order_type: str
    status: str
    payment_status: str
    table_number: str | None = None
    customer_name: str | None = None
    notes: str | None = None
    subtotal: float
    tax: float
    discount: float
    total: float
    amount_paid: float
    balance: float
    item_count: int
    server_name: str | None = None
    created_at: datetime
    items: list[OrderItemOut] | None = None
    payments: list[PaymentOut] | None = None


# --- Billing ----------------------------------------------------------------
class PayRequest(BaseModel):
    phone: str | None = None


class SubscriptionOut(BaseModel):
    id: str
    status: str
    price: float
    currency: str
    in_trial: bool
    has_access: bool
    trial_ends_at: datetime | None = None
    current_period_end: datetime | None = None
    failed_attempts: int
    next_retry_at: datetime | None = None


class ChargeOut(BaseModel):
    id: str
    status: str
    amount: float
    currency: str
    period_start: datetime
    period_end: datetime
    provider_receipt: str | None = None
    result_desc: str | None = None
    attempt_number: int
    requested_at: datetime
    finalized_at: datetime | None = None
