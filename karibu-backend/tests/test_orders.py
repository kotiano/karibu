"""Ordering, payment and the arithmetic an owner reconciles against."""


def test_create_order_computes_its_own_total(client, owner):
    """The client sends quantities, never prices. If the server trusted a
    posted total, anyone could pay whatever they liked."""
    r = client.post("/api/orders", headers=owner["headers"], json={
        "order_type": "dine_in",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": 3}],
    })
    assert r.status_code in (200, 201)
    o = r.json()["data"]
    assert o["total"] == 1500      # 3 x 500, computed server-side
    assert o["balance"] == 1500
    assert o["payment_status"] == "unpaid"


def test_payment_cannot_exceed_the_balance(client, owner):
    """Overpaying would inflate reported revenue — the figure the owner uses to
    decide everything else."""
    o = client.post("/api/orders", headers=owner["headers"], json={
        "order_type": "dine_in",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": 1}],
    }).json()["data"]

    r = client.post(f"/api/orders/{o['id']}/payments", headers=owner["headers"],
                    json={"method": "cash", "amount": 999999})
    assert r.status_code == 422


def test_split_payment_settles_the_order(client, owner):
    o = client.post("/api/orders", headers=owner["headers"], json={
        "order_type": "dine_in",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": 2}],
    }).json()["data"]

    client.post(f"/api/orders/{o['id']}/payments", headers=owner["headers"],
                json={"method": "cash", "amount": 400})
    r = client.post(f"/api/orders/{o['id']}/payments", headers=owner["headers"],
                    json={"method": "mpesa", "amount": 600})
    assert r.status_code in (200, 201)

    o2 = client.get(f"/api/orders/{o['id']}", headers=owner["headers"]).json()["data"]
    assert o2["amount_paid"] == 1000
    assert o2["payment_status"] == "paid"


def test_credit_is_not_revenue_until_collected(client, owner):
    """The whole point of the debt feature. A credit sale creates a Debt, NOT a
    Payment, so unpaid credit can never inflate today's sales."""
    before = client.get("/api/analytics/sales?days=1", headers=owner["headers"]).json()["data"]["gross_revenue"]

    o = client.post("/api/orders", headers=owner["headers"], json={
        "order_type": "dine_in",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": 2}],
    }).json()["data"]
    client.patch(f"/api/orders/{o['id']}/status", headers=owner["headers"], json={"status": "served"})
    r = client.post(f"/api/orders/{o['id']}/payments", headers=owner["headers"], json={
        "method": "debt", "amount": o["total"],
        "customer_name": "Wanjiru", "customer_phone": "0712345678",
        "due_date": "2026-12-31T12:00:00.000Z",
    })
    assert r.status_code in (200, 201)

    after = client.get("/api/analytics/sales?days=1", headers=owner["headers"]).json()["data"]["gross_revenue"]
    assert after == before, "credit must not count as revenue"

    debts = client.get("/api/debts", headers=owner["headers"]).json()["data"]
    assert debts["total_outstanding"] >= 1000


def test_collecting_a_debt_turns_it_into_revenue(client, owner):
    o = client.post("/api/orders", headers=owner["headers"], json={
        "order_type": "dine_in",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": 1}],
    }).json()["data"]
    client.patch(f"/api/orders/{o['id']}/status", headers=owner["headers"], json={"status": "served"})
    client.post(f"/api/orders/{o['id']}/payments", headers=owner["headers"], json={
        "method": "debt", "amount": 500, "customer_name": "Otieno",
        "due_date": "2026-12-31T12:00:00.000Z",
    })

    debt = [d for d in client.get("/api/debts", headers=owner["headers"]).json()["data"]["debts"]
            if d["customer_name"] == "Otieno"][0]

    before = client.get("/api/analytics/sales?days=1", headers=owner["headers"]).json()["data"]["gross_revenue"]
    r = client.post(f"/api/debts/{debt['id']}/pay", headers=owner["headers"],
                    json={"amount": 500, "method": "mpesa"})
    assert r.status_code == 200
    after = client.get("/api/analytics/sales?days=1", headers=owner["headers"]).json()["data"]["gross_revenue"]
    assert after == before + 500, "collection must land in sales on the day it's collected"


def test_debt_repayment_cannot_exceed_what_is_owed(client, owner):
    o = client.post("/api/orders", headers=owner["headers"], json={
        "order_type": "dine_in",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": 1}],
    }).json()["data"]
    client.patch(f"/api/orders/{o['id']}/status", headers=owner["headers"], json={"status": "served"})
    client.post(f"/api/orders/{o['id']}/payments", headers=owner["headers"], json={
        "method": "debt", "amount": 500, "customer_name": "Njoroge",
        "due_date": "2026-12-31T12:00:00.000Z",
    })
    debt = [d for d in client.get("/api/debts", headers=owner["headers"]).json()["data"]["debts"]
            if d["customer_name"] == "Njoroge"][0]

    r = client.post(f"/api/debts/{debt['id']}/pay", headers=owner["headers"],
                    json={"amount": 5000, "method": "cash"})
    assert r.status_code == 422


def test_credit_requires_a_customer_name(client, owner):
    """A debt nobody can be identified from is uncollectable."""
    o = client.post("/api/orders", headers=owner["headers"], json={
        "order_type": "dine_in",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": 1}],
    }).json()["data"]
    r = client.post(f"/api/orders/{o['id']}/payments", headers=owner["headers"],
                    json={"method": "debt", "amount": 500})
    assert r.status_code == 422


def test_due_date_with_a_browser_timezone_is_accepted(client, owner):
    """Browsers send ISO-8601 ending in Z. That used to be parsed as a
    timezone-AWARE datetime and rejected by Postgres, 500ing every credit sale."""
    o = client.post("/api/orders", headers=owner["headers"], json={
        "order_type": "dine_in",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": 1}],
    }).json()["data"]
    client.patch(f"/api/orders/{o['id']}/status", headers=owner["headers"], json={"status": "served"})
    r = client.post(f"/api/orders/{o['id']}/payments", headers=owner["headers"], json={
        "method": "debt", "amount": 500, "customer_name": "Z-Timezone",
        "due_date": "2026-12-31T21:30:00.000Z",
    })
    assert r.status_code in (200, 201)
    # And listing must not blow up comparing naive utcnow() to an aware value.
    assert client.get("/api/debts", headers=owner["headers"]).status_code == 200
