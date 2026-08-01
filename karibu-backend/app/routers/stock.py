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
from app.models import (
    Expense, ExpenseCategory, MenuItem, MovementReason, RecipeLine, StockItem,
    StockMovement, StockUnit, UserRole,
)
from app.schemas.common import ok
from app.schemas.stock import (
    MovementCreate, RecipeSet, StockItemCreate, StockItemUpdate, to_milli,
)

router = APIRouter(prefix="/api/stock", tags=["stock"])

MANAGERS = UserRole.MANAGERS


def _item_dict(item: StockItem, recipes: list | None = None) -> dict:
    lines = recipes or []
    # Portions remaining, measured by the dish that eats the MOST per plate —
    # the conservative reading. Reporting the most generous one would promise
    # servings the shelf cannot cover if the wrong dish is ordered.
    heaviest = max((r.quantity_milli for r in lines), default=0)
    return {
        "dish_count": len(lines),
        "portions_left": (
            round(item.quantity_milli / heaviest, 1) if heaviest > 0 else None
        ),
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

    # One query for every recipe line on this restaurant's items, grouped in
    # Python — a pantry is tens of rows, and per-item queries would be a loop
    # of round trips for a list screen.
    recipe_res = await db.execute(
        select(RecipeLine).where(
            RecipeLine.stock_item_id.in_([i.id for i in items] or [""])
        )
    )
    by_item: dict[str, list] = {}
    for line in recipe_res.scalars().all():
        by_item.setdefault(line.stock_item_id, []).append(line)

    # is_low is a Python property (it depends on two columns and a "0 disables
    # it" rule), so the filter happens here. The list is one restaurant's
    # pantry — tens of rows, not thousands.
    low = [i for i in items if i.is_low]
    if low_only:
        items = low

    # None where no item has a cost, rather than 0 — "we do not track cost" and
    # "the stock is worthless" are different statements.
    dishes_res = await db.execute(
        select(MenuItem)
        .where(
            MenuItem.restaurant_id == user.restaurant_id,
            MenuItem.is_archived.is_(False),
        )
        .order_by(MenuItem.name.asc())
    )

    priced = [i for i in items if i.unit_cost_cents is not None]
    total_value = sum(i.value_cents or 0 for i in priced) if priced else None

    return ok(
        {
            "items": [_item_dict(i, by_item.get(i.id)) for i in items],
            "low_count": len(low),
            "total_value": total_value / 100 if total_value is not None else None,
            "units": list(StockUnit.ALL),
            "reasons": list(MovementReason.MANUAL),
            # So "which dish does this make?" can be asked while ADDING the
            # ingredient, rather than only in a separate step afterwards.
            "menu_items": [
                {"id": m.id, "name": m.name} for m in dishes_res.scalars().all()
            ],
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

    # Linked to a dish here if the yield was given, so a new ingredient starts
    # deducting immediately instead of needing a second trip through another
    # screen — which is where it was, and where nobody found it.
    linked = None
    if body.menu_item_id and body.portions_per_unit:
        dish = (
            await db.execute(
                select(MenuItem).where(
                    MenuItem.id == body.menu_item_id,
                    MenuItem.restaurant_id == user.restaurant_id,
                )
            )
        ).scalar_one_or_none()
        if not dish:
            raise APIError("Menu item not found", status=404,
                           errors={"menu_item_id": "unknown"})
        recipe = RecipeLine(
            menu_item_id=dish.id,
            stock_item_id=item.id,
            quantity_milli=round(1000 / body.portions_per_unit),
        )
        db.add(recipe)
        linked = dish.name

    await db.commit()
    await db.refresh(item)
    # Passed through so the response reports the link it just made — otherwise
    # the card says "won't deduct on sale" until the next reload.
    return ok(
        _item_dict(item, [recipe] if linked else None),
        message=(
            f"{item.name} added to stock"
            if not linked
            else f"{item.name} added — one {linked} now uses "
                 f"{1 / body.portions_per_unit:g} {item.unit}"
        ),
    )


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
    cost_cents = round(body.cost * 100) if body.cost else None

    movement = StockMovement(
        stock_item_id=item.id,
        delta_milli=delta,
        reason=body.reason,
        note=(body.note or "").strip() or None,
        balance_after_milli=new_balance,
        total_cost_cents=cost_cents,
        recorded_by_id=user.id,
    )
    db.add(movement)

    # A COSTED DELIVERY WRITES ITS OWN EXPENSE. Recording the purchase twice by
    # hand — once here, once on the expenses screen — is how the two drifted:
    # forget the expense and costs are understated, forget the delivery and the
    # shelf count is wrong. One action, both records, joined by id.
    expense = None
    if cost_cents and body.reason == MovementReason.RECEIVED:
        supplier = (body.supplier or "").strip() or item.supplier
        expense = Expense(
            restaurant_id=user.restaurant_id,
            category=ExpenseCategory.STOCK,
            amount_cents=cost_cents,
            payee=supplier,
            note=f"{abs(delta) / 1000:g} {item.unit} {item.name}",
            method="cash" if body.paid else "credit",
            is_paid=body.paid,
            paid_at=datetime.utcnow() if body.paid else None,
            recorded_by_id=user.id,
        )
        db.add(expense)
        await db.flush()
        expense.stock_movement_id = movement.id

    # Item, movement and expense commit TOGETHER. Separate commits could leave
    # a running total with no ledger entry, or a delivery with no cost.
    await db.commit()
    await db.refresh(item)
    return ok(
        {**_item_dict(item), "expense_created": expense is not None},
        message=(
            "Stock updated"
            if expense is None
            else ("Stock and expense recorded" if body.paid
                  else "Stock recorded — added to what you owe suppliers")
        ),
    )


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


# ---------------------------------------------------------------------------
# Recipes: what a dish takes off the shelf when it sells.
# ---------------------------------------------------------------------------
@router.get("/{item_id}/recipes", dependencies=[Depends(require_roles(*MANAGERS))])
async def list_recipes(item_id: str, user: SubscribedUser, db: DbDep):
    """Which dishes consume this ingredient, and how much each takes."""
    item = await _get_item(db, user, item_id)

    result = await db.execute(
        select(RecipeLine)
        .join(MenuItem, RecipeLine.menu_item_id == MenuItem.id)
        .where(RecipeLine.stock_item_id == item.id)
        .options(selectinload(RecipeLine.menu_item))
        .order_by(MenuItem.name.asc())
    )
    lines = result.scalars().all()

    # Only dishes this restaurant sells, and not the archived ones — a picker
    # offering retired items would let someone wire up a recipe that can never
    # fire.
    dishes_res = await db.execute(
        select(MenuItem)
        .where(
            MenuItem.restaurant_id == user.restaurant_id,
            MenuItem.is_archived.is_(False),
        )
        .order_by(MenuItem.name.asc())
    )

    return ok(
        {
            "item": _item_dict(item),
            "recipes": [
                {
                    "id": r.id,
                    "menu_item_id": r.menu_item_id,
                    "menu_item_name": r.menu_item.name if r.menu_item else None,
                    "quantity": r.quantity,
                    # The number the owner actually thinks in: how many plates
                    # one whole unit yields. Derived, never stored — storing
                    # both invites them to disagree.
                    "portions_per_unit": round(1000 / r.quantity_milli, 2)
                    if r.quantity_milli
                    else None,
                }
                for r in lines
            ],
            "menu_items": [
                {"id": m.id, "name": m.name} for m in dishes_res.scalars().all()
            ],
        }
    )


@router.post("/{item_id}/recipes", dependencies=[Depends(require_roles(*MANAGERS))])
async def set_recipe(
    item_id: str, body: RecipeSet, user: SubscribedUser, db: DbDep
):
    """Set (or clear) how much of this ingredient one sale of a dish uses.

    Accepts EITHER `portions_per_unit` — "this kg makes 4 plates" — or an
    explicit `quantity`. The first is the number an owner knows; the second is
    what actually gets stored. Converting here means the arithmetic happens
    once, in one place, rather than in whichever screen asked.
    """
    item = await _get_item(db, user, item_id)

    dish = (
        await db.execute(
            select(MenuItem).where(
                MenuItem.id == body.menu_item_id,
                MenuItem.restaurant_id == user.restaurant_id,
            )
        )
    ).scalar_one_or_none()
    if not dish:
        raise APIError("Menu item not found", status=404)

    if body.portions_per_unit is not None:
        quantity_milli = round(1000 / body.portions_per_unit)
    else:
        quantity_milli = to_milli(body.quantity or 0)

    existing = (
        await db.execute(
            select(RecipeLine).where(
                RecipeLine.stock_item_id == item.id,
                RecipeLine.menu_item_id == dish.id,
            )
        )
    ).scalar_one_or_none()

    # Zero means "this dish does not use this ingredient" — the natural way to
    # undo a link, rather than a separate delete endpoint the UI has to know.
    if quantity_milli <= 0:
        if existing:
            await db.delete(existing)
            await db.commit()
        return ok(message=f"{dish.name} no longer uses {item.name}")

    if existing:
        existing.quantity_milli = quantity_milli
    else:
        db.add(
            RecipeLine(
                menu_item_id=dish.id,
                stock_item_id=item.id,
                quantity_milli=quantity_milli,
            )
        )
    await db.commit()

    per_sale = quantity_milli / 1000
    return ok(
        message=(
            f"One {dish.name} now uses {per_sale:g} {item.unit} of {item.name}"
        )
    )
