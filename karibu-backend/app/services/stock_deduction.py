"""Deduct stock when dishes are sold, and give it back when they are not.

WHEN: at order creation, because that is when the kitchen starts cooking and
therefore when the food is actually gone. Deducting at payment would leave the
shelf overstated all through service, which is exactly when someone is deciding
whether there is enough meat left for another table.

CANCELLING RESTORES IT, for the same reason — a voided order is food that was
never cooked. The restore is its own movement rather than a deletion, so the
ledger shows both and neither is silently rewritten.

A SALE MAY TAKE STOCK NEGATIVE, unlike a hand-keyed movement. Refusing would
mean the till stops taking orders because a pantry count is stale — the food is
physically there, the book is wrong. A negative balance is the honest signal
that the two have diverged, and it is visible on the stock screen, whereas
blocking the sale would just look like the app is broken mid-service.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import MovementReason, Order, RecipeLine, StockItem, StockMovement


async def _lines_for(db: AsyncSession, order: Order) -> list[tuple[StockItem, int]]:
    """(stock item, milli to move) for one order, netted per item.

    Netted because two dishes on the same ticket may share an ingredient, and
    two movements against one item in a single transaction would race on
    balance_after and produce a ledger that does not reconcile.
    """
    menu_ids = [i.menu_item_id for i in order.items if i.menu_item_id]
    if not menu_ids:
        return []

    result = await db.execute(
        select(RecipeLine)
        .where(RecipeLine.menu_item_id.in_(menu_ids))
        .options(selectinload(RecipeLine.stock_item))
    )
    recipes = result.scalars().all()
    if not recipes:
        return []

    by_menu: dict[str, list[RecipeLine]] = {}
    for r in recipes:
        by_menu.setdefault(r.menu_item_id, []).append(r)

    totals: dict[str, int] = {}
    items: dict[str, StockItem] = {}
    for line in order.items:
        for r in by_menu.get(line.menu_item_id, []):
            # Archived ingredients stop consuming: the item is no longer being
            # tracked, and driving a hidden balance is worse than not tracking.
            if r.stock_item is None or r.stock_item.is_archived:
                continue
            totals[r.stock_item_id] = (
                totals.get(r.stock_item_id, 0) + r.quantity_milli * line.quantity
            )
            items[r.stock_item_id] = r.stock_item

    return [(items[sid], milli) for sid, milli in totals.items() if milli]


async def apply_sale(db: AsyncSession, order: Order, user_id: str | None) -> int:
    """Consume ingredients for a newly placed order. Returns items affected."""
    return await _move(db, order, user_id, sign=-1, note=f"Sold · {order.reference}")


async def reverse_sale(db: AsyncSession, order: Order, user_id: str | None) -> int:
    """Give the ingredients back when an order is cancelled."""
    return await _move(db, order, user_id, sign=1, note=f"Cancelled · {order.reference}")


async def _move(
    db: AsyncSession, order: Order, user_id: str | None, *, sign: int, note: str
) -> int:
    moved = 0
    for item, milli in await _lines_for(db, order):
        delta = sign * milli
        item.quantity_milli += delta
        db.add(
            StockMovement(
                stock_item_id=item.id,
                delta_milli=delta,
                reason=MovementReason.SALE,
                note=note,
                balance_after_milli=item.quantity_milli,
                recorded_by_id=user_id,
            )
        )
        moved += 1
    # No commit here — the caller owns the transaction, so the deduction lands
    # with the order itself or not at all.
    return moved
