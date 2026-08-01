"""Customer receipts.

The public endpoint is the interesting one: it has no authentication by design,
so the tests are mostly about what that token may and may not reach.
"""
from tests.conftest import register_and_confirm


def place_and_pay(client, owner, pay=True):
    order = client.post("/api/orders", json={
        "order_type": "dine_in", "table_number": "7",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": 2}],
    }, headers=owner["headers"]).json()["data"]
    if pay:
        r = client.post(f"/api/orders/{order['id']}/payments",
                        json={"method": "mpesa", "amount": 1000},
                        headers=owner["headers"])
        assert r.status_code == 201, r.text
    return order


def issue(client, owner, order):
    r = client.post(f"/api/orders/{order['id']}/receipt", headers=owner["headers"])
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_receipt_is_readable_without_signing_in(client, owner):
    """The customer has no account, so the link must work on its own."""
    order = place_and_pay(client, owner)
    link = issue(client, owner, order)

    # No Authorization header at all.
    r = client.get(f"/api/receipt/{link['token']}")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["reference"] == order["reference"]
    assert data["total"] == 1000
    assert data["is_paid"] is True
    assert data["items"][0]["quantity"] == 2


def test_issuing_twice_returns_the_same_link(client, owner):
    """A customer who lost the message needs THEIR receipt, not a new one that
    makes the first look like a different sale."""
    order = place_and_pay(client, owner)
    assert issue(client, owner, order)["token"] == issue(client, owner, order)["token"]


def test_an_unpaid_order_is_marked_unpaid(client, owner):
    """A pro-forma must not be waveable as proof of payment."""
    order = place_and_pay(client, owner, pay=False)
    data = client.get(f"/api/receipt/{issue(client, owner, order)['token']}").json()["data"]
    assert data["is_paid"] is False
    assert data["balance"] == 1000


def test_a_wrong_token_reveals_nothing(client, owner):
    place_and_pay(client, owner)
    assert client.get("/api/receipt/" + "z" * 43).status_code == 404


def test_the_token_reaches_exactly_one_order(client, owner):
    """The sharpest risk of a public endpoint: one link exposing the shop."""
    first = place_and_pay(client, owner)
    second = place_and_pay(client, owner)
    data = client.get(f"/api/receipt/{issue(client, owner, first)['token']}").json()["data"]
    assert data["reference"] == first["reference"]
    assert data["reference"] != second["reference"]


def test_another_restaurant_cannot_issue_a_receipt_for_your_order(client, mail_log, owner):
    order = place_and_pay(client, owner)
    _, _, other = register_and_confirm(client, mail_log, restaurant="Rival Cafe")
    r = client.post(f"/api/orders/{order['id']}/receipt",
                    headers={"Authorization": f"Bearer {other['tokens']['access_token']}"})
    assert r.status_code == 404


def test_a_waiter_can_issue_a_receipt(client, owner):
    """Whoever closed the table is exactly who gets asked for one."""
    from tests.test_staff import onboard

    _, waiter, _ = onboard(client, owner, role="waiter", name="Otieno",
                           phone="0744000111")
    order = client.post("/api/orders", json={
        "order_type": "takeaway",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": 1}],
    }, headers=waiter).json()["data"]

    r = client.post(f"/api/orders/{order['id']}/receipt", headers=waiter)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["url"].endswith(r.json()["data"]["token"])


def test_receipt_names_who_served_and_how_it_was_paid(client, owner):
    order = place_and_pay(client, owner)
    data = client.get(f"/api/receipt/{issue(client, owner, order)['token']}").json()["data"]
    assert data["served_by"] == "Test Owner"
    assert data["payments"][0]["method"] == "mpesa"
    assert data["restaurant"] == "Test Kitchen"
