"""Stock purchases and expenses are one event, not two.

Buying stock used to need two unlinked entries — a movement and an expense —
which drifted apart in both directions. And a delivery taken on credit is a
cost immediately but not money out of the till, which the expense table could
not express.
"""


def make_item(client, owner, **kw):
    body = {"name": "Cooking oil", "unit": "l", "quantity": 0, "reorder_level": 5}
    body.update(kw)
    r = client.post("/api/stock", json=body, headers=owner["headers"])
    assert r.status_code == 201, r.text
    return r.json()["data"]


def deliver(client, owner, item, *, qty=20, cost=6000, paid=True, supplier=None):
    r = client.post(f"/api/stock/{item['id']}/movements", json={
        "reason": "received", "quantity": qty, "cost": cost,
        "paid": paid, "supplier": supplier,
    }, headers=owner["headers"])
    assert r.status_code == 201, r.text
    return r.json()["data"]


def expenses(client, owner):
    return client.get("/api/expenses?days=1", headers=owner["headers"]).json()["data"]


def test_a_costed_delivery_records_the_expense_itself(client, owner):
    """One action, both records — no keying the same purchase twice."""
    item = make_item(client, owner)
    result = deliver(client, owner, item, supplier="Soko Market")
    assert result["expense_created"] is True
    assert result["quantity"] == 20

    e = expenses(client, owner)
    assert len(e["expenses"]) == 1
    row = e["expenses"][0]
    assert row["category"] == "stock"
    assert row["amount"] == 6000
    assert row["payee"] == "Soko Market"
    assert row["from_stock"] is True
    # And it names what was bought, not just a number.
    assert "Cooking oil" in (row["note"] or "")


def test_a_delivery_with_no_cost_records_no_expense(client, owner):
    """Plenty of kitchens track quantities and not money."""
    item = make_item(client, owner)
    r = client.post(f"/api/stock/{item['id']}/movements",
                    json={"reason": "received", "quantity": 5},
                    headers=owner["headers"])
    assert r.status_code == 201
    assert r.json()["data"]["expense_created"] is False
    assert expenses(client, owner)["expenses"] == []


def test_using_stock_is_not_an_expense(client, owner):
    """Consuming what you already bought is not a second purchase."""
    item = make_item(client, owner, quantity=10)
    r = client.post(f"/api/stock/{item['id']}/movements",
                    json={"reason": "used", "quantity": -3, "cost": 900},
                    headers=owner["headers"])
    assert r.status_code == 201
    assert r.json()["data"]["expense_created"] is False


def test_stock_on_credit_is_a_cost_but_not_cash_out(client, owner):
    """The distinction the expense table could not previously express."""
    item = make_item(client, owner)
    deliver(client, owner, item, cost=6000, paid=False, supplier="Soko Market")

    e = expenses(client, owner)
    assert e["total"] == 6000, "the cost is real the moment the goods arrive"
    assert e["total_paid"] == 0, "but no money has left the till"
    assert e["total_unpaid"] == 6000, "and it is owed to a supplier"
    assert e["expenses"][0]["is_paid"] is False


def test_paying_a_supplier_moves_it_from_owed_to_spent(client, owner):
    item = make_item(client, owner)
    deliver(client, owner, item, cost=6000, paid=False)
    expense_id = expenses(client, owner)["expenses"][0]["id"]

    r = client.post(f"/api/expenses/{expense_id}/pay", headers=owner["headers"])
    assert r.status_code == 200, r.text

    e = expenses(client, owner)
    assert e["total"] == 6000, "the cost does not move — it was counted on arrival"
    assert e["total_paid"] == 6000
    assert e["total_unpaid"] == 0


def test_a_supplier_cannot_be_paid_twice(client, owner):
    item = make_item(client, owner)
    deliver(client, owner, item, cost=6000, paid=False)
    eid = expenses(client, owner)["expenses"][0]["id"]

    assert client.post(f"/api/expenses/{eid}/pay", headers=owner["headers"]).status_code == 200
    assert client.post(f"/api/expenses/{eid}/pay", headers=owner["headers"]).status_code == 422


def test_a_cash_delivery_counts_as_spent_immediately(client, owner):
    item = make_item(client, owner)
    deliver(client, owner, item, cost=6000, paid=True)
    e = expenses(client, owner)
    assert e["total_paid"] == 6000
    assert e["total_unpaid"] == 0


def test_the_expense_points_back_at_the_delivery(client, owner):
    """The join that stops the two records drifting."""
    item = make_item(client, owner)
    deliver(client, owner, item)

    history = client.get(f"/api/stock/{item['id']}/movements",
                         headers=owner["headers"]).json()["data"]
    assert history["movements"][0]["reason"] == "received"
    assert expenses(client, owner)["expenses"][0]["from_stock"] is True


def test_manually_recorded_expenses_still_count_as_paid(client, owner):
    """The default has to stay 'paid' — that is what every existing expense
    meant, and what typing one into the form still means."""
    r = client.post("/api/expenses", json={
        "category": "rent", "amount": 30000, "payee": "Landlord",
    }, headers=owner["headers"])
    assert r.status_code == 201, r.text
    e = expenses(client, owner)
    assert e["total_paid"] == 30000
    assert e["total_unpaid"] == 0
