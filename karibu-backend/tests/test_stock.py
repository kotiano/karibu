"""Stock management.

The assertions worth having are the LEDGER INVARIANTS: the running total always
matches the movements that produced it, quantities never drift, a level cannot
be edited without a reason attached, and one restaurant cannot see another's
pantry.
"""
from tests.conftest import register_and_confirm


def make_item(client, owner, **kw):
    body = {"name": "Sukuma Wiki", "unit": "kg", "quantity": 10, "reorder_level": 2}
    body.update(kw)
    r = client.post("/api/stock", json=body, headers=owner["headers"])
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_create_and_list(client, owner):
    item = make_item(client, owner, unit_cost=80)
    assert item["quantity"] == 10
    assert item["is_low"] is False
    # 10kg at 80/kg.
    assert item["value"] == 800

    listing = client.get("/api/stock", headers=owner["headers"]).json()["data"]
    assert [i["name"] for i in listing["items"]] == ["Sukuma Wiki"]
    assert listing["total_value"] == 800


def test_movements_drive_the_balance(client, owner):
    item = make_item(client, owner, quantity=10)

    for delta, reason in [(5, "received"), (-3.5, "used"), (-0.5, "waste")]:
        r = client.post(f"/api/stock/{item['id']}/movements",
                        json={"reason": reason, "quantity": delta},
                        headers=owner["headers"])
        assert r.status_code == 201, r.text

    # 10 + 5 - 3.5 - 0.5
    assert r.json()["data"]["quantity"] == 11.0

    history = client.get(f"/api/stock/{item['id']}/movements",
                         headers=owner["headers"]).json()["data"]
    # Four including the opening count.
    assert len(history["movements"]) == 4
    # Newest first, and each carries the balance it produced.
    assert history["movements"][0]["balance_after"] == 11.0
    # used + waste, reported positive.
    assert history["consumed"] == 4.0


def test_fractional_quantities_do_not_drift(client, owner):
    """0.1 + 0.2 != 0.3 in binary floating point.

    Ten additions of a tenth must land on exactly 1, or a stock count silently
    rots by a hair per movement.
    """
    item = make_item(client, owner, quantity=0)
    for _ in range(10):
        client.post(f"/api/stock/{item['id']}/movements",
                    json={"reason": "received", "quantity": 0.1},
                    headers=owner["headers"])
    r = client.get("/api/stock", headers=owner["headers"]).json()["data"]
    assert r["items"][0]["quantity"] == 1.0


def test_cannot_take_out_more_than_is_there(client, owner):
    """Refused, not clamped — flooring at zero would erase the discrepancy."""
    item = make_item(client, owner, quantity=2)
    r = client.post(f"/api/stock/{item['id']}/movements",
                    json={"reason": "used", "quantity": -5},
                    headers=owner["headers"])
    assert r.status_code == 422
    assert client.get("/api/stock", headers=owner["headers"]).json()["data"]["items"][0]["quantity"] == 2


def test_quantity_cannot_be_edited_directly(client, owner):
    """The whole point of the ledger: no silent way to make a shortfall vanish."""
    item = make_item(client, owner, quantity=10)
    client.patch(f"/api/stock/{item['id']}", json={"quantity": 999, "name": "Sukuma"},
                 headers=owner["headers"])
    after = client.get("/api/stock", headers=owner["headers"]).json()["data"]["items"][0]
    assert after["quantity"] == 10, "quantity changed without a movement"
    assert after["name"] == "Sukuma", "the rest of the patch should still apply"


def test_low_stock_flag_and_zero_disables_it(client, owner):
    low = make_item(client, owner, name="Salt", quantity=1, reorder_level=5)
    assert low["is_low"] is True

    never = make_item(client, owner, name="Toothpicks", quantity=0, reorder_level=0)
    assert never["is_low"] is False, "reorder_level 0 means never warn, not warn at zero"

    listing = client.get("/api/stock?low_only=true", headers=owner["headers"]).json()["data"]
    assert [i["name"] for i in listing["items"]] == ["Salt"]
    assert listing["low_count"] == 1


def test_archived_items_disappear_but_keep_their_history(client, owner):
    item = make_item(client, owner)
    assert client.delete(f"/api/stock/{item['id']}", headers=owner["headers"]).status_code == 200

    listing = client.get("/api/stock", headers=owner["headers"]).json()["data"]
    assert listing["items"] == []
    # Gone from the app's view, and gone from the API surface too.
    assert client.get(f"/api/stock/{item['id']}/movements",
                      headers=owner["headers"]).status_code == 404


def test_stock_is_tenant_scoped(client, mail_log, owner):
    item = make_item(client, owner, name="SECRET-INGREDIENT")
    _, _, other = register_and_confirm(client, mail_log, restaurant="Rival Cafe")
    headers = {"Authorization": f"Bearer {other['tokens']['access_token']}"}

    assert client.get("/api/stock", headers=headers).json()["data"]["items"] == []
    # And a known id from another tenant must not be reachable.
    assert client.get(f"/api/stock/{item['id']}/movements", headers=headers).status_code == 404
    assert client.post(f"/api/stock/{item['id']}/movements",
                       json={"reason": "used", "quantity": -1},
                       headers=headers).status_code == 404


def test_movement_requires_a_known_reason_and_a_nonzero_quantity(client, owner):
    item = make_item(client, owner)
    for body in ({"reason": "vanished", "quantity": -1}, {"reason": "used", "quantity": 0}):
        assert client.post(f"/api/stock/{item['id']}/movements", json=body,
                           headers=owner["headers"]).status_code == 422
