"""Stock (inventory) routes.

Owner/manager only, the same gate expenses use — stock levels and their cost are
the same class of commercial information.

THE CENTRAL RULE: a stock level is only ever changed by recording a movement.
There is no endpoint that sets a quantity directly. That is what makes the
ledger trustworthy: every difference between yesterday's figure and today's has
a reason, a time and a person attached, and a shortfall cannot be quietly
edited away.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.dependencies import DbDep, SubscribedUser, require_roles
from app.core.security import APIError
from app.models import MovementReason, StockItem, StockMovement, StockUnit, UserRole
from app.schemas.common import ok
from app.schemas.stock import MovementCreate, StockItemCreate, StockItemUpdate, to_milli

router = APIRouter(prefix="/api/stock", tags=["stock"])

MANAGERS = (UserRole.OWNER, UserRole.MANAGER)


def _item_dict(item: StockItem) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "unit": item.unit,
        "quantity": item.quantity,
        "reorder_level": item.reorder_level,
        "unit_cost": item.unit_cost_cents / 100 if item.unit_cost_cents is not None else None,
        "value": item.value_cents / 100 if item.value_cents is not None else None,
        "supplier": item.supplier,
        "note": item.note,
        "is_low": item.is_low,
        "updated_at": item.updated_at,
    }


def _movement_dict(m: StockMovement) -> dict:
    return {
        "id": m.id,
        "delta": m.delta,
        "reason": m.reason,
        "note": m.note,
        "balance_after": m.balance_after,
        "recorded_by": m.recorded_by.full_name if m.recorded_by else None,
        "occurred_at": m.occurred_at,
    }


async def _get_item(db, user, item_id: str) -> StockItem:
    """Fetch scoped to the caller's restaurant.

    The tenant check is in the WHERE clause, not an if-statement after the
    fetch — a 404 and a 403 leak the same fact here, and filtering means a
    cross-tenant id simply does not exist.
    """
    result = await db.execute(
        select(StockItem).where(
            StockItem.id == item_id,
            StockItem.restaurant_id == user.restaurant_id,
        )
    )
    item = result.scalar_one_or_none()
    if not item or item.is_archived:
        raise APIError("Stock item not found", status=404)
    return item


@router.get("", dependencies=[Depends(require_roles(*MANAGERS))])
async def list_stock(
    user: SubscribedUser,
    db: DbDep,
    low_only: bool = False,
):
    """Everything on the shelf, plus what it is worth and what is running out."""
    result = await db.execute(
        select(StockItem)
        .where(
            StockItem.restaurant_id == user.restaurant_id,
            StockItem.is_archived.is_(False),
        )
        .order_by(StockItem.name.asc())
    )
    items = list(result.scalars().all())

    # is_low is a Python property (it depends on two columns and a "0 disables
    # it" rule), so the filter happens here. The list is one restaurant's
    # pantry — tens of rows, not thousands.
    low = [i for i in items if i.is_low]
    if low_only:
        items = low

    # None where no item has a cost, rather than 0 — "we do not track cost" and
    # "the stock is worthless" are different statements.
    priced = [i for i in items if i.unit_cost_cents is not None]
    total_value = sum(i.value_cents or 0 for i in priced) if priced else None

    return ok(
        {
            "items": [_item_dict(i) for i in items],
            "low_count": len(low),
            "total_value": total_value / 100 if total_value is not None else None,
            "units": list(StockUnit.ALL),
            "reasons": list(MovementReason.ALL),
        }
    )


@router.post("", status_code=201, dependencies=[Depends(require_roles(*MANAGERS))])
async def create_stock_item(body: StockItemCreate, user: SubscribedUser, db: DbDep):
    item = StockItem(
        restaurant_id=user.restaurant_id,
        name=body.name.strip(),
        unit=body.unit,
        quantity_milli=to_milli(body.quantity),
        reorder_level_milli=to_milli(body.reorder_level),
        unit_cost_cents=round(body.unit_cost * 100) if body.unit_cost is not None else None,
        supplier=(body.supplier or "").strip() or None,
        note=(body.note or "").strip() or None,
    )
    db.add(item)
    await db.flush()

    # An opening quantity is itself a movement, so the ledger starts where the
    # item does and replaying it always reproduces the current figure.
    if item.quantity_milli:
        db.add(
            StockMovement(
                stock_item_id=item.id,
                delta_milli=item.quantity_milli,
                reason=MovementReason.COUNT,
                note="Opening stock",
                balance_after_milli=item.quantity_milli,
                recorded_by_id=user.id,
            )
        )

    await db.commit()
    await db.refresh(item)
    return ok(_item_dict(item), message=f"{item.name} added to stock")


@router.patch("/{item_id}", dependencies=[Depends(require_roles(*MANAGERS))])
async def update_stock_item(
    item_id: str, body: StockItemUpdate, user: SubscribedUser, db: DbDep
):
    item = await _get_item(db, user, item_id)

    if body.name is not None:
        item.name = body.name.strip()
    if body.unit is not None:
        item.unit = body.unit
    if body.reorder_level is not None:
        item.reorder_level_milli = to_milli(body.reorder_level)
    if body.unit_cost is not None:
        item.unit_cost_cents = round(body.unit_cost * 100)
    if body.supplier is not None:
        item.supplier = body.supplier.strip() or None
    if body.note is not None:
        item.note = body.note.strip() or None

    await db.commit()
    await db.refresh(item)
    return ok(_item_dict(item), message="Stock item updated")


@router.delete("/{item_id}", dependencies=[Depends(require_roles(*MANAGERS))])
async def archive_stock_item(item_id: str, user: SubscribedUser, db: DbDep):
    """Archive, never delete — the movements are the record of what happened."""
    item = await _get_item(db, user, item_id)
    item.is_archived = True
    await db.commit()
    return ok(message=f"{item.name} removed from stock")


@router.post("/{item_id}/movements", status_code=201,
             dependencies=[Depends(require_roles(*MANAGERS))])
async def record_movement(
    item_id: str, body: MovementCreate, user: SubscribedUser, db: DbDep
):
    """Record a delivery, usage, waste, return or a corrected count."""
    item = await _get_item(db, user, item_id)

    delta = to_milli(body.quantity)
    new_balance = item.quantity_milli + delta
    if new_balance < 0:
        # Refused rather than clamped. A kitchen that has used more than the
        # book says has a real discrepancy, and silently flooring at zero would
        # erase exactly the evidence this ledger exists to keep.
        raise APIError(
            f"That would leave {item.name} at {new_balance / 1000:g} {item.unit}. "
            f"There is {item.quantity:g} {item.unit} on record — "
            f"record a count first if the figure is wrong.",
            status=422,
            errors={"quantity": "More than the recorded stock"},
        )

    item.quantity_milli = new_balance
    db.add(
        StockMovement(
            stock_item_id=item.id,
            delta_milli=delta,
            reason=body.reason,
            note=(body.note or "").strip() or None,
            balance_after_milli=new_balance,
            recorded_by_id=user.id,
        )
    )
    # Item and movement commit TOGETHER. Two commits could leave the running
    # total updated with no ledger entry explaining it.
    await db.commit()
    await db.refresh(item)
    return ok(_item_dict(item), message="Stock updated")


@router.get("/{item_id}/movements", dependencies=[Depends(require_roles(*MANAGERS))])
async def list_movements(
    item_id: str,
    user: SubscribedUser,
    db: DbDep,
    days: int = Query(default=90, ge=1, le=366),
    limit: int = Query(default=100, ge=1, le=500),
):
    """The history for one item, newest first."""
    item = await _get_item(db, user, item_id)
    since = datetime.utcnow() - timedelta(days=days)

    result = await db.execute(
        select(StockMovement)
        .where(
            StockMovement.stock_item_id == item.id,
            StockMovement.occurred_at >= since,
        )
        .options(selectinload(StockMovement.recorded_by))
        .order_by(StockMovement.occurred_at.desc())
        .limit(limit)
    )
    movements = result.scalars().all()

    used_res = await db.execute(
        select(func.coalesce(func.sum(StockMovement.delta_milli), 0)).where(
            StockMovement.stock_item_id == item.id,
            StockMovement.occurred_at >= since,
            StockMovement.reason.in_(MovementReason.OUTGOING),
        )
    )
    return ok(
        {
            "item": _item_dict(item),
            "movements": [_movement_dict(m) for m in movements],
            # Reported as a positive number: "12kg went out", not "-12".
            "consumed": abs((used_res.scalar() or 0)) / 1000,
            "range_days": days,
        }
    )
