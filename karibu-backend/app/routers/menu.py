"""Menu routes: browse and manage categories/items. Tenant-scoped + gated."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.cache import cache_delete, cache_get, cache_set
from app.core.config import settings
from app.core.dependencies import DbDep, SubscribedUser, require_roles, require_subscription
from app.core.security import APIError
from app.core.serializers import menu_item_dict
from app.models import Category, MenuItem, OrderItem, UserRole
from app.schemas.common import ok
from app.schemas.menu import CategoryCreate, CategoryUpdate, MenuItemCreate, MenuItemUpdate

router = APIRouter(prefix="/api/menu", tags=["menu"])

MANAGERS = (UserRole.OWNER, UserRole.MANAGER)


def _categories_cache_key(restaurant_id: str) -> str:
    return f"menu:categories:{restaurant_id}"


@router.get("/categories")
async def list_categories(user: SubscribedUser, db: DbDep):
    """Categories with available items nested, for the caller's restaurant.

    This is the single most-hit read in the app — every staff member browses
    it while building every order — so it's cached per-tenant for a short TTL
    (cache-aside: read through on miss, invalidated on any write below).
    """
    cache_key = _categories_cache_key(user.restaurant_id)
    cached = await cache_get(cache_key)
    if cached is not None:
        return ok(cached)

    result = await db.execute(
        select(Category)
        .where(Category.restaurant_id == user.restaurant_id)
        .options(selectinload(Category.items))
        .order_by(Category.sort_order)
    )
    cats = result.scalars().all()
    data = []
    for c in cats:
        data.append(
            {
                "id": c.id,
                "name": c.name,
                "icon": c.icon,
                "sort_order": c.sort_order,
                # Archived items are retired: excluded from the count as well as
                # the list, or a category reads "3 items" while showing 2.
                "item_count": sum(1 for i in c.items if not i.is_archived),
                "items": [
                    menu_item_dict(i)
                    for i in c.items
                    if i.is_available and not i.is_archived
                ],
            }
        )
    await cache_set(cache_key, data, settings.CACHE_MENU_TTL_SECONDS)
    return ok(data)


@router.get("/items")
async def list_items(
    user: SubscribedUser,
    db: DbDep,
    category_id: str | None = None,
    search: str | None = None,
    available_only: bool = Query(default=False),
):
    # Archived items are retired from the menu entirely — they exist only to keep
    # historical order lines referential, so they never appear in a listing.
    stmt = select(MenuItem).where(
        MenuItem.restaurant_id == user.restaurant_id,
        MenuItem.is_archived.is_(False),
    )
    if category_id:
        stmt = stmt.where(MenuItem.category_id == category_id)
    if available_only:
        stmt = stmt.where(MenuItem.is_available.is_(True))
    if search:
        stmt = stmt.where(MenuItem.name.ilike(f"%{search}%"))
    stmt = stmt.options(selectinload(MenuItem.category)).order_by(MenuItem.name)

    result = await db.execute(stmt)
    items = result.scalars().all()
    return ok([menu_item_dict(i) for i in items])


@router.get("/items/{item_id}")
async def get_item(item_id: str, user: SubscribedUser, db: DbDep):
    item = await _get_scoped_item(db, item_id, user.restaurant_id)
    return ok(menu_item_dict(item))


@router.post("/categories", status_code=201)
async def create_category(
    body: CategoryCreate,
    db: DbDep,
    user=Depends(require_roles(*MANAGERS)),
    _sub=Depends(require_subscription),
):
    cat = Category(
        name=body.name.strip(),
        icon=body.icon,
        sort_order=body.sort_order,
        restaurant_id=user.restaurant_id,
    )
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    await cache_delete(_categories_cache_key(user.restaurant_id))
    return ok(
        {
            "id": cat.id,
            "name": cat.name,
            "icon": cat.icon,
            "sort_order": cat.sort_order,
            "item_count": 0,
        },
        message="Category created",
    )


@router.patch("/categories/{category_id}")
async def update_category(
    category_id: str,
    body: CategoryUpdate,
    db: DbDep,
    user=Depends(require_roles(*MANAGERS)),
    _sub=Depends(require_subscription),
):
    cat = await _get_scoped_category(db, category_id, user.restaurant_id)
    if body.name is not None:
        cat.name = body.name.strip()
    if body.icon is not None:
        cat.icon = body.icon
    if body.sort_order is not None:
        cat.sort_order = body.sort_order
    await db.commit()
    await db.refresh(cat, attribute_names=["items"])
    await cache_delete(_categories_cache_key(user.restaurant_id))
    return ok(
        {
            "id": cat.id,
            "name": cat.name,
            "icon": cat.icon,
            "sort_order": cat.sort_order,
            "item_count": len(cat.items),
        },
        message="Category updated",
    )


@router.delete("/categories/{category_id}")
async def delete_category(
    category_id: str,
    db: DbDep,
    user=Depends(require_roles(*MANAGERS)),
    _sub=Depends(require_subscription),
):
    """Delete a category. Refuses if it still holds items, so existing orders
    (which snapshot item names/prices) are never orphaned by surprise.

    Archived items block deletion too, even though the owner can no longer see
    them. Category.items cascades delete-orphan, so removing the category would
    try to hard-delete those rows — and they only exist to keep historical order
    lines referential, so that raises the same ForeignKeyViolationError the item
    endpoint used to 500 on. They get their own message, because
    "move your items first" is baffling when the category looks empty.
    """
    cat = await _get_scoped_category(db, category_id, user.restaurant_id)

    counts = (
        await db.execute(
            select(MenuItem.is_archived, func.count())
            .where(MenuItem.category_id == cat.id)
            .group_by(MenuItem.is_archived)
        )
    ).all()
    live = next((n for archived, n in counts if not archived), 0)
    archived = next((n for archived, n in counts if archived), 0)

    if live:
        raise APIError(
            "Move or delete this category's items first.",
            status=409,
            errors={"category": "not empty"},
        )
    if archived:
        raise APIError(
            f"This category still holds {archived} retired item(s) kept for your "
            f"sales history, so it can't be deleted. You can rename it instead.",
            status=409,
            errors={"category": "has_archived_items"},
        )
    await db.delete(cat)
    await db.commit()
    await cache_delete(_categories_cache_key(user.restaurant_id))
    return ok(message="Category deleted")


@router.post("/items", status_code=201)
async def create_item(
    body: MenuItemCreate,
    db: DbDep,
    user=Depends(require_roles(*MANAGERS)),
    _sub=Depends(require_subscription),
):
    cat = await _get_scoped_category(db, body.category_id, user.restaurant_id)
    item = MenuItem(
        name=body.name.strip(),
        description=body.description,
        price_cents=int(round(body.price * 100)),
        image_url=body.image_url,
        prep_minutes=body.prep_minutes,
        is_available=body.is_available,
        category_id=cat.id,
        restaurant_id=user.restaurant_id,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item, attribute_names=["category"])
    await cache_delete(_categories_cache_key(user.restaurant_id))
    return ok(menu_item_dict(item), message="Menu item created")


@router.patch("/items/{item_id}")
async def update_item(
    item_id: str,
    body: MenuItemUpdate,
    db: DbDep,
    user=Depends(require_roles(*MANAGERS)),
    _sub=Depends(require_subscription),
):
    item = await _get_scoped_item(db, item_id, user.restaurant_id)

    if body.name is not None:
        item.name = body.name.strip()
    if body.description is not None:
        item.description = body.description
    if body.price is not None:
        item.price_cents = int(round(body.price * 100))
    if body.image_url is not None:
        item.image_url = body.image_url
    if body.prep_minutes is not None:
        item.prep_minutes = body.prep_minutes
    if body.is_available is not None:
        item.is_available = body.is_available
    if body.category_id is not None:
        await _get_scoped_category(db, body.category_id, user.restaurant_id)
        item.category_id = body.category_id

    await db.commit()
    await db.refresh(item, attribute_names=["category"])
    await cache_delete(_categories_cache_key(user.restaurant_id))
    return ok(menu_item_dict(item), message="Menu item updated")


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str,
    db: DbDep,
    user=Depends(require_roles(*MANAGERS)),
    _sub=Depends(require_subscription),
):
    """Remove an item from the menu.

    Two paths, because order_items.menu_item_id is a non-nullable FK to this
    table:

    - Never ordered  -> a real DELETE. Nothing references it, so leave no residue.
    - Ordered at least once -> ARCHIVE it. A hard delete raises
      ForeignKeyViolationError (which is what used to surface as a 500), and
      cascading would be worse than the crash: it would erase order lines from
      paid historical orders, silently changing past totals and corrupting the
      sales history the restaurant does its books from. Archiving takes the item
      off every menu surface while keeping history intact.

    Either way the caller gets 200 and a message describing what happened.
    """
    item = await _get_scoped_item(
        db, item_id, user.restaurant_id, include_archived=True
    )

    if item.is_archived:
        raise APIError("This item is already removed from the menu", status=409)

    times_ordered = (
        await db.execute(
            select(func.count())
            .select_from(OrderItem)
            .where(OrderItem.menu_item_id == item.id)
        )
    ).scalar() or 0

    if times_ordered:
        item.is_archived = True
        # An archived item must not linger as "out of stock" on any surface that
        # only checks availability.
        item.is_available = False
        message = (
            f"'{item.name}' removed from the menu. It stays on "
            f"{times_ordered} past order(s) so your sales history is unchanged."
        )
    else:
        await db.delete(item)
        message = f"'{item.name}' deleted."

    await db.commit()
    await cache_delete(_categories_cache_key(user.restaurant_id))
    return ok(message=message)


# --- Scoped fetch helpers (tenant isolation) --------------------------------
async def _get_scoped_item(
    db, item_id: str, restaurant_id: str, *, include_archived: bool = False
) -> MenuItem:
    """Fetch an item within the caller's restaurant.

    Archived items are invisible by default: they're retired, and existing only
    to keep historical order lines referential doesn't make them editable. Only
    delete_item passes include_archived, so it can answer "already removed"
    rather than a misleading 404.
    """
    conditions = [MenuItem.id == item_id, MenuItem.restaurant_id == restaurant_id]
    if not include_archived:
        conditions.append(MenuItem.is_archived.is_(False))

    result = await db.execute(
        select(MenuItem).where(*conditions).options(selectinload(MenuItem.category))
    )
    item = result.scalar_one_or_none()
    if not item:
        raise APIError("Menu item not found", status=404)
    return item


async def _get_scoped_category(db, category_id: str, restaurant_id: str) -> Category:
    result = await db.execute(
        select(Category).where(
            Category.id == category_id, Category.restaurant_id == restaurant_id
        )
    )
    cat = result.scalar_one_or_none()
    if not cat:
        raise APIError("Category not found", status=404)
    return cat
