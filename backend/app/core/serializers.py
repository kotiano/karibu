"""ORM → response-dict serializers.

FastAPI + Pydantic can serialize simple models directly, but Order/Subscription
have computed fields (balance, item_count, in_trial, …) that are cleaner to build
explicitly. Keeping them here matches the shape the existing frontend expects.
"""
from app.models import (
    BillingCharge,
    MenuItem,
    Order,
    Subscription,
)


def menu_item_dict(item: MenuItem) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "price": item.price,
        "price_cents": item.price_cents,
        "image_url": item.image_url,
        "is_available": item.is_available,
        "prep_minutes": item.prep_minutes,
        "category_id": item.category_id,
        "category_name": item.category.name if item.category else None,
    }


def order_dict(order: Order, detailed: bool = True) -> dict:
    data = {
        "id": order.id,
        "reference": order.reference,
        "order_type": order.order_type,
        "status": order.status,
        "payment_status": order.payment_status,
        "table_number": order.table_number,
        "customer_name": order.customer_name,
        "notes": order.notes,
        "subtotal": round(order.subtotal_cents / 100, 2),
        "discount": round(order.discount_cents / 100, 2),
        "total": round(order.total_cents / 100, 2),
        "amount_paid": round(order.amount_paid_cents / 100, 2),
        "balance": round(order.balance_cents / 100, 2),
        "item_count": sum(li.quantity for li in order.items),
        "server_name": order.server.full_name if order.server else None,
        "created_at": order.created_at,
    }
    if detailed:
        data["items"] = [
            {
                "id": li.id,
                "menu_item_id": li.menu_item_id,
                "name": li.name_snapshot,
                "unit_price": round(li.unit_price_cents / 100, 2),
                "quantity": li.quantity,
                "modifiers": li.modifiers,
                "line_total": round(li.line_total_cents / 100, 2),
            }
            for li in order.items
        ]
        data["payments"] = [
            {
                "id": p.id,
                "method": p.method,
                "amount": round(p.amount_cents / 100, 2),
                "reference": p.reference,
                "received_at": p.received_at,
            }
            for p in order.payments
        ]
    return data


def subscription_dict(sub: Subscription) -> dict:
    return {
        "id": sub.id,
        "status": sub.status,
        "price": round(sub.price_cents / 100, 2),
        "currency": sub.currency,
        "in_trial": sub.in_trial,
        "has_access": sub.has_access,
        "trial_ends_at": sub.trial_ends_at,
        "current_period_end": sub.current_period_end,
        "failed_attempts": sub.failed_attempts,
        "next_retry_at": sub.next_retry_at,
    }


def charge_dict(charge: BillingCharge) -> dict:
    return {
        "id": charge.id,
        "status": charge.status,
        "amount": round(charge.amount_cents / 100, 2),
        "currency": charge.currency,
        "period_start": charge.period_start,
        "period_end": charge.period_end,
        "mpesa_receipt": charge.mpesa_receipt,
        "result_desc": charge.result_desc,
        "attempt_number": charge.attempt_number,
        "requested_at": charge.requested_at,
        "finalized_at": charge.finalized_at,
    }
