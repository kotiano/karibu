"""Who is answerable for money that has not come in.

The feature exists so an unpaid order has a name against it. These tests check
that the name is right and, just as importantly, that the two exposures stay
SEPARATE — a table still eating is not the same thing as a customer who has not
paid in three weeks.
"""


def place_order(client, owner, qty=2):
    r = client.post("/api/orders", json={
        "order_type": "dine_in",
        "table_number": "5",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": qty}],
    }, headers=owner["headers"])
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_orders_record_who_served_them(client, owner):
    order = place_order(client, owner)
    assert order["server_name"] == "Test Owner"

    fetched = client.get(f"/api/orders/{order['id']}", headers=owner["headers"]).json()["data"]
    assert fetched["server_name"] == "Test Owner"


def test_unpaid_order_is_attributed_to_its_server(client, owner):
    place_order(client, owner)  # 2 x 500 = 1000, unpaid

    report = client.get("/api/analytics/accountability", headers=owner["headers"]).json()["data"]
    row = next(r for r in report["staff"] if r["name"] == "Test Owner")
    assert row["orders_served"] == 1
    assert row["unpaid_orders"] == 1
    assert row["unpaid_value"] == 1000
    assert report["total_unpaid"] == 1000


def test_paying_an_order_clears_the_exposure(client, owner):
    order = place_order(client, owner)
    r = client.post(f"/api/orders/{order['id']}/payments",
                    json={"method": "cash", "amount": 1000},
                    headers=owner["headers"])
    assert r.status_code == 201, r.text

    report = client.get("/api/analytics/accountability", headers=owner["headers"]).json()["data"]
    assert report["total_unpaid"] == 0


def test_credit_is_attributed_to_whoever_authorised_it(client, owner):
    order = place_order(client, owner)
    r = client.post(f"/api/orders/{order['id']}/payments", json={
        "method": "debt", "amount": 1000, "customer_name": "Mama Ali",
    }, headers=owner["headers"])
    assert r.status_code == 201, r.text

    debts = client.get("/api/debts", headers=owner["headers"]).json()["data"]["debts"]
    assert debts[0]["recorded_by"] == "Test Owner"
    assert debts[0]["served_by"] == "Test Owner"

    report = client.get("/api/analytics/accountability", headers=owner["headers"]).json()["data"]
    row = next(r for r in report["staff"] if r["name"] == "Test Owner")
    assert row["credit_given"] == 1
    assert row["credit_outstanding"] == 1000


def test_credit_and_unpaid_are_not_merged(client, owner):
    """A table mid-service and a three-week-old debt must stay distinguishable.

    An order settled as credit is fully 'paid' as far as the order is concerned
    — the exposure moved to the debt. Reporting it in both columns would double
    count it.
    """
    place_order(client, owner)                       # left unpaid
    on_credit = place_order(client, owner)
    client.post(f"/api/orders/{on_credit['id']}/payments", json={
        "method": "debt", "amount": 1000, "customer_name": "Mama Ali",
    }, headers=owner["headers"])

    report = client.get("/api/analytics/accountability", headers=owner["headers"]).json()["data"]
    row = next(r for r in report["staff"] if r["name"] == "Test Owner")
    assert row["unpaid_orders"] == 1, "the credit order should not also count as unpaid"
    assert row["credit_given"] == 1
    assert report["total_unpaid"] == 1000
    assert report["total_credit_outstanding"] == 1000


def test_settling_a_debt_clears_the_credit_exposure(client, owner):
    order = place_order(client, owner)
    client.post(f"/api/orders/{order['id']}/payments", json={
        "method": "debt", "amount": 1000, "customer_name": "Mama Ali",
    }, headers=owner["headers"])
    debt = client.get("/api/debts", headers=owner["headers"]).json()["data"]["debts"][0]

    r = client.post(f"/api/debts/{debt['id']}/pay", json={"amount": 1000},
                    headers=owner["headers"])
    assert r.status_code == 200, r.text

    report = client.get("/api/analytics/accountability", headers=owner["headers"]).json()["data"]
    assert report["total_credit_outstanding"] == 0


def test_cancelled_orders_are_not_held_against_anyone(client, owner):
    order = place_order(client, owner)
    r = client.patch(f"/api/orders/{order['id']}/status", json={"status": "cancelled"},
                     headers=owner["headers"])
    assert r.status_code == 200, r.text

    report = client.get("/api/analytics/accountability", headers=owner["headers"]).json()["data"]
    assert report["total_unpaid"] == 0
