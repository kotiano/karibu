"""Menu routes: browse and manage categories/items. Tenant-scoped + gated."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.cache import cache_delete, cache_get, cache_set
from app.core.config import settings
from app.core.dependencies import DbDep, SubscribedUser, require_roles, require_subscription
from app.core.security import APIError
from app.core.serializers import menu_item_dict
from app.models import Category, MenuItem, UserRole
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
                "item_count": len(c.items),
                "items": [menu_item_dict(i) for i in c.items if i.is_available],
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
    stmt = select(MenuItem).where(MenuItem.restaurant_id == user.restaurant_id)
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
    """Delete a category. Refuses if it still has items, so existing orders
    (which snapshot item names/prices) are never orphaned by surprise."""
    cat = await _get_scoped_category(db, category_id, user.restaurant_id)
    count_res = await db.execute(
        select(func.count()).select_from(MenuItem).where(MenuItem.category_id == cat.id)
    )
    if (count_res.scalar() or 0) > 0:
        raise APIError(
            "Move or delete this category's items first.",
            status=409,
            errors={"category": "not empty"},
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
    item = await _get_scoped_item(db, item_id, user.restaurant_id)
    await db.delete(item)
    await db.commit()
    await cache_delete(_categories_cache_key(user.restaurant_id))
    return ok(message="Menu item deleted")


# --- Scoped fetch helpers (tenant isolation) --------------------------------
async def _get_scoped_item(db, item_id: str, restaurant_id: str) -> MenuItem:
    result = await db.execute(
        select(MenuItem)
        .where(MenuItem.id == item_id, MenuItem.restaurant_id == restaurant_id)
        .options(selectinload(MenuItem.category))
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
