"""Menu lifecycle and subscription billing."""


def test_deleting_an_unsold_item_removes_it(client, owner):
    item = client.post("/api/menu/items", headers=owner["headers"], json={
        "name": "Never Ordered", "price": 100, "category_id": owner["category"]["id"],
    }).json()["data"]

    r = client.delete(f"/api/menu/items/{item['id']}", headers=owner["headers"])
    assert r.status_code == 200
    assert "deleted" in r.json()["message"].lower()

    names = [i["name"] for c in client.get("/api/menu/categories", headers=owner["headers"]).json()["data"]
             for i in (c.get("items") or [])]
    assert "Never Ordered" not in names


def test_deleting_a_sold_item_keeps_the_order_intact(client, owner):
    """The bug this exists for: a hard delete violated the order_items foreign
    key and 500'd, and cascading would have erased order lines from PAID
    orders — silently changing past totals."""
    item = client.post("/api/menu/items", headers=owner["headers"], json={
        "name": "Sold Once", "price": 700, "category_id": owner["category"]["id"],
    }).json()["data"]

    order = client.post("/api/orders", headers=owner["headers"], json={
        "order_type": "dine_in", "items": [{"menu_item_id": item["id"], "quantity": 2}],
    }).json()["data"]
    client.patch(f"/api/orders/{order['id']}/status", headers=owner["headers"], json={"status": "served"})
    client.post(f"/api/orders/{order['id']}/payments", headers=owner["headers"],
                json={"method": "cash", "amount": order["total"]})

    r = client.delete(f"/api/menu/items/{item['id']}", headers=owner["headers"])
    assert r.status_code == 200, "removing a sold item must not error"

    after = client.get(f"/api/orders/{order['id']}", headers=owner["headers"]).json()["data"]
    assert after["total"] == 1400, "a paid order's total must not change"
    assert after["items"][0]["name"] == "Sold Once"

    names = [i["name"] for c in client.get("/api/menu/categories", headers=owner["headers"]).json()["data"]
             for i in (c.get("items") or [])]
    assert "Sold Once" not in names, "an archived item must leave the menu"


def test_an_archived_item_cannot_be_sold_again(client, owner):
    """A client holding a stale menu must not be able to sell something the
    owner has retired."""
    item = client.post("/api/menu/items", headers=owner["headers"], json={
        "name": "Retired", "price": 300, "category_id": owner["category"]["id"],
    }).json()["data"]
    o = client.post("/api/orders", headers=owner["headers"], json={
        "order_type": "dine_in", "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }).json()["data"]
    client.patch(f"/api/orders/{o['id']}/status", headers=owner["headers"], json={"status": "served"})
    client.post(f"/api/orders/{o['id']}/payments", headers=owner["headers"],
                json={"method": "cash", "amount": o["total"]})
    client.delete(f"/api/menu/items/{item['id']}", headers=owner["headers"])

    r = client.post("/api/orders", headers=owner["headers"], json={
        "order_type": "dine_in", "items": [{"menu_item_id": item["id"], "quantity": 1}],
    })
    assert r.status_code == 404


def test_deleting_an_item_twice_is_a_clean_conflict(client, owner):
    item = client.post("/api/menu/items", headers=owner["headers"], json={
        "name": "Twice", "price": 200, "category_id": owner["category"]["id"],
    }).json()["data"]
    o = client.post("/api/orders", headers=owner["headers"], json={
        "order_type": "dine_in", "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }).json()["data"]
    client.patch(f"/api/orders/{o['id']}/status", headers=owner["headers"], json={"status": "served"})
    client.post(f"/api/orders/{o['id']}/payments", headers=owner["headers"],
                json={"method": "cash", "amount": o["total"]})

    assert client.delete(f"/api/menu/items/{item['id']}", headers=owner["headers"]).status_code == 200
    assert client.delete(f"/api/menu/items/{item['id']}", headers=owner["headers"]).status_code == 409


def test_new_restaurant_starts_on_a_trial(client, owner):
    sub = client.get("/api/billing/subscription", headers=owner["headers"]).json()["data"]["subscription"]
    assert sub["status"] == "trialing"
    assert sub["has_access"] is True


def test_pay_is_idempotent(client, owner):
    """Double-tapping Pay must not create a second charge — the DB's partial
    unique index is the hard stop, and this proves it holds through the API."""
    first = client.post("/api/billing/pay", headers=owner["headers"], json={}).json()["data"]["charge"]
    second = client.post("/api/billing/pay", headers=owner["headers"], json={}).json()["data"]["charge"]
    assert first["id"] == second["id"], "a second Pay must reuse the open charge"

    charges = client.get("/api/billing/charges", headers=owner["headers"]).json()["data"]["charges"]
    assert len([c for c in charges if c["status"] in ("pending", "processing")]) == 1
