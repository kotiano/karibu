"""Selling a dish takes its ingredients off the shelf.

The entry question is "how many plates does this make"; the stored fact is how
much one plate consumes. These tests check the conversion, the deduction, and
the cases where getting it wrong would cost real stock.
"""


def stock_item(client, owner, **kw):
    body = {"name": "Beef", "unit": "kg", "quantity": 10, "reorder_level": 2}
    body.update(kw)
    r = client.post("/api/stock", json=body, headers=owner["headers"])
    assert r.status_code == 201, r.text
    return r.json()["data"]


def set_recipe(client, owner, item, menu_item_id, **kw):
    r = client.post(f"/api/stock/{item['id']}/recipes",
                   json={"menu_item_id": menu_item_id, **kw},
                   headers=owner["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def order(client, owner, menu_item_id, qty=1, headers=None):
    r = client.post("/api/orders", json={
        "order_type": "dine_in", "table_number": "4",
        "items": [{"menu_item_id": menu_item_id, "quantity": qty}],
    }, headers=headers or owner["headers"])
    assert r.status_code == 201, r.text
    return r.json()["data"]


def level(client, owner, item_id):
    listing = client.get("/api/stock", headers=owner["headers"]).json()["data"]
    return next(i["quantity"] for i in listing["items"] if i["id"] == item_id)


def test_yield_converts_to_a_per_plate_amount(client, owner):
    """'10kg makes 40 plates' is 4 plates per kg, so 0.25kg a plate."""
    item = stock_item(client, owner)
    set_recipe(client, owner, item, owner["item"]["id"], portions_per_unit=4)

    r = client.get(f"/api/stock/{item['id']}/recipes",
                   headers=owner["headers"]).json()["data"]
    assert r["recipes"][0]["quantity"] == 0.25
    assert r["recipes"][0]["portions_per_unit"] == 4


def test_selling_a_dish_deducts_the_ingredient(client, owner):
    item = stock_item(client, owner, quantity=10)
    set_recipe(client, owner, item, owner["item"]["id"], portions_per_unit=4)

    order(client, owner, owner["item"]["id"], qty=2)   # 2 plates x 0.25kg
    assert level(client, owner, item["id"]) == 9.5


def test_the_deduction_is_recorded_as_a_sale_with_the_order_on_it(client, owner):
    item = stock_item(client, owner)
    set_recipe(client, owner, item, owner["item"]["id"], portions_per_unit=4)
    o = order(client, owner, owner["item"]["id"])

    history = client.get(f"/api/stock/{item['id']}/movements",
                         headers=owner["headers"]).json()["data"]
    latest = history["movements"][0]
    assert latest["reason"] == "sale"
    assert o["reference"] in (latest["note"] or "")
    assert latest["delta"] == -0.25


def test_cancelling_an_order_puts_the_ingredients_back(client, owner):
    item = stock_item(client, owner, quantity=10)
    set_recipe(client, owner, item, owner["item"]["id"], portions_per_unit=4)
    o = order(client, owner, owner["item"]["id"], qty=4)
    assert level(client, owner, item["id"]) == 9.0

    assert client.delete(f"/api/orders/{o['id']}", headers=owner["headers"]).status_code == 200
    assert level(client, owner, item["id"]) == 10.0


def test_cancelling_twice_does_not_invent_stock(client, owner):
    """The failure that would quietly manufacture inventory."""
    item = stock_item(client, owner, quantity=10)
    set_recipe(client, owner, item, owner["item"]["id"], portions_per_unit=4)
    o = order(client, owner, owner["item"]["id"], qty=4)

    assert client.delete(f"/api/orders/{o['id']}", headers=owner["headers"]).status_code == 200
    assert client.delete(f"/api/orders/{o['id']}", headers=owner["headers"]).status_code == 409
    assert level(client, owner, item["id"]) == 10.0


def test_a_dish_with_no_recipe_deducts_nothing(client, owner):
    """Most menus will be partly wired up. The rest must simply not move."""
    item = stock_item(client, owner, quantity=10)
    order(client, owner, owner["item"]["id"], qty=3)
    assert level(client, owner, item["id"]) == 10.0


def test_a_sale_may_take_stock_negative(client, owner):
    """Refusing would stop the till because a pantry count is stale — the food
    is there, the book is wrong. A negative balance says exactly that."""
    item = stock_item(client, owner, quantity=1)
    set_recipe(client, owner, item, owner["item"]["id"], portions_per_unit=4)

    order(client, owner, owner["item"]["id"], qty=8)   # 2kg from 1kg
    assert level(client, owner, item["id"]) == -1.0


def test_two_dishes_sharing_an_ingredient_net_to_one_movement(client, owner):
    """Two movements against one item in a transaction would race on
    balance_after and leave a ledger that does not reconcile."""
    second = client.post("/api/menu/items", json={
        "name": "Beef Stew", "price": 400, "category_id": owner["category"]["id"],
    }, headers=owner["headers"]).json()["data"]

    item = stock_item(client, owner, quantity=10)
    set_recipe(client, owner, item, owner["item"]["id"], portions_per_unit=4)   # 0.25
    set_recipe(client, owner, item, second["id"], portions_per_unit=10)         # 0.1

    r = client.post("/api/orders", json={
        "order_type": "dine_in",
        "items": [
            {"menu_item_id": owner["item"]["id"], "quantity": 2},
            {"menu_item_id": second["id"], "quantity": 3},
        ],
    }, headers=owner["headers"])
    assert r.status_code == 201, r.text

    # 2*0.25 + 3*0.1 = 0.8
    assert level(client, owner, item["id"]) == 9.2
    history = client.get(f"/api/stock/{item['id']}/movements",
                         headers=owner["headers"]).json()["data"]
    sales = [m for m in history["movements"] if m["reason"] == "sale"]
    assert len(sales) == 1, "the shared ingredient must move once, netted"
    assert sales[0]["delta"] == -0.8
    assert sales[0]["balance_after"] == 9.2


def test_clearing_a_recipe_stops_the_deduction(client, owner):
    item = stock_item(client, owner, quantity=10)
    set_recipe(client, owner, item, owner["item"]["id"], portions_per_unit=4)
    set_recipe(client, owner, item, owner["item"]["id"], quantity=0)

    order(client, owner, owner["item"]["id"], qty=4)
    assert level(client, owner, item["id"]) == 10.0


def test_a_sale_cannot_be_keyed_by_hand(client, owner):
    """Otherwise the automatic figure is editable, which defeats it."""
    item = stock_item(client, owner)
    r = client.post(f"/api/stock/{item['id']}/movements",
                    json={"reason": "sale", "quantity": -1},
                    headers=owner["headers"])
    assert r.status_code == 422


def test_recipes_are_tenant_scoped(client, mail_log, owner):
    from tests.conftest import register_and_confirm

    item = stock_item(client, owner)
    _, _, other = register_and_confirm(client, mail_log, restaurant="Rival Cafe")
    h = {"Authorization": f"Bearer {other['tokens']['access_token']}"}

    assert client.get(f"/api/stock/{item['id']}/recipes", headers=h).status_code == 404
    assert client.post(f"/api/stock/{item['id']}/recipes",
                      json={"menu_item_id": owner["item"]["id"], "portions_per_unit": 4},
                      headers=h).status_code == 404
