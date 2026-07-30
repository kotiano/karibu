"""Model package — importing here registers every table on the Base metadata."""
from app.models.audit import AuditAction, AuditLog
from app.models.base import BaseModel
from app.models.debt import Debt, DebtStatus
from app.models.menu import Category, MenuItem
from app.models.order import (
    Order,
    OrderItem,
    OrderStatus,
    OrderType,
    Payment,
    PaymentMethod,
    PaymentStatus,
)
from app.models.restaurant import Restaurant
from app.models.subscription import (
    BillingCharge,
    ChargeStatus,
    ProcessedCallback,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import User, UserRole

__all__ = [
    "BaseModel",
    "Restaurant",
    "User",
    "UserRole",
    "Category",
    "MenuItem",
    "Order",
    "OrderItem",
    "Payment",
    "OrderStatus",
    "OrderType",
    "PaymentMethod",
    "PaymentStatus",
    "Subscription",
    "SubscriptionStatus",
    "BillingCharge",
    "ChargeStatus",
    "ProcessedCallback",
    "AuditLog",
    "AuditAction",
    "Debt",
    "DebtStatus",
]
