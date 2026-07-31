"""Expenses — the other half of the profit figure."""


def test_record_and_list_an_expense(client, owner):
    r = client.post("/api/expenses", headers=owner["headers"], json={
        "category": "stock", "amount": 4500, "payee": "Mama Mboga", "method": "mpesa",
    })
    assert r.status_code == 201
    assert r.json()["data"]["amount"] == 4500

    d = client.get("/api/expenses?days=30", headers=owner["headers"]).json()["data"]
    assert d["total"] >= 4500
    assert d["by_category"]["stock"] >= 4500


def test_unknown_category_is_refused(client, owner):
    """Categories are a fixed list, not free text — free-typed ones become
    'Rent', 'rent' and 'RENT ' within a week and the breakdown stops adding up."""
    r = client.post("/api/expenses", headers=owner["headers"],
                    json={"category": "definitely-not-real", "amount": 100})
    assert r.status_code == 422


def test_amount_must_be_positive(client, owner):
    for bad in (0, -50):
        r = client.post("/api/expenses", headers=owner["headers"],
                        json={"category": "other", "amount": bad})
        assert r.status_code == 422


def test_backdating_with_a_browser_timezone(client, owner):
    """An owner keys Friday's costs in on Monday. Dating them Monday would
    misstate both days — and the Z-suffixed timestamp a browser sends used to
    be rejected outright by Postgres."""
    r = client.post("/api/expenses", headers=owner["headers"], json={
        "category": "utilities", "amount": 3200, "payee": "KPLC",
        "spent_at": "2026-07-20T12:00:00.000Z",
    })
    assert r.status_code == 201
    assert r.json()["data"]["spent_at"].startswith("2026-07-20")


def test_window_filtering_excludes_older_expenses(client, owner):
    client.post("/api/expenses", headers=owner["headers"], json={
        "category": "rent", "amount": 25000, "spent_at": "2020-01-01T12:00:00.000Z",
    })
    d = client.get("/api/expenses?days=7", headers=owner["headers"]).json()["data"]
    assert "rent" not in d["by_category"], "an expense from 2020 must not land in a 7-day window"


def test_update_and_delete(client, owner):
    e = client.post("/api/expenses", headers=owner["headers"],
                    json={"category": "repairs", "amount": 1500}).json()["data"]

    r = client.patch(f"/api/expenses/{e['id']}", headers=owner["headers"], json={"amount": 1800})
    assert r.status_code == 200 and r.json()["data"]["amount"] == 1800

    assert client.delete(f"/api/expenses/{e['id']}", headers=owner["headers"]).status_code == 200
    assert client.patch(f"/api/expenses/{e['id']}", headers=owner["headers"],
                        json={"amount": 1}).status_code == 404


def test_expenses_are_scoped_to_the_restaurant(client, owner, mail_log):
    from tests.conftest import register_and_confirm

    e = client.post("/api/expenses", headers=owner["headers"],
                    json={"category": "rent", "amount": 99999, "payee": "Private"}).json()["data"]

    _, _, other = register_and_confirm(client, mail_log, restaurant="Nosy Kitchen")
    ho = {"Authorization": f"Bearer {other['tokens']['access_token']}"}

    assert client.patch(f"/api/expenses/{e['id']}", headers=ho, json={"amount": 1}).status_code == 404
    assert client.get("/api/expenses", headers=ho).json()["data"]["total"] == 0
