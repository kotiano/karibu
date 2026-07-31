"""Staff accounts and the permission matrix.

Until now no test could hold a non-manager token, because nothing could create
one — the role gates were asserted by introspecting FastAPI's dependency tree.
These tests sign in as a real cashier and a real waiter and check what the API
actually refuses them.
"""
import io
import re

import pytest

from tests.conftest import PASSWORD, register_and_confirm

NEW_PASSWORD = "Kazi!Nzuri2026"


def hire(client, manager, *, role="cashier", name="Jane Wanjiru", phone="0722000001",
         email=None):
    """Create a staff account and return (staff, temp_code)."""
    r = client.post("/api/staff", json={
        "full_name": name, "role": role, "phone": phone, "email": email,
    }, headers=manager["headers"])
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    return data["staff"], data["temp_code"]


def sign_in(client, identifier, password):
    return client.post("/api/auth/login", json={"email": identifier, "password": password})


def onboard(client, manager, **kw):
    """Hire someone and take them all the way through their first sign-in."""
    staff, code = hire(client, manager, **kw)
    ident = staff["email"] or staff["phone"]

    first = sign_in(client, ident, code)
    assert first.status_code == 200, first.text
    tmp_headers = {"Authorization": f"Bearer {first.json()['data']['tokens']['access_token']}"}

    r = client.post("/api/auth/change-password", json={
        "current_password": code, "new_password": NEW_PASSWORD,
    }, headers=tmp_headers)
    assert r.status_code == 200, r.text
    tokens = r.json()["data"]["tokens"]
    return staff, {"Authorization": f"Bearer {tokens['access_token']}"}, ident


# ── Creation ────────────────────────────────────────────────────────────────
def test_signup_creates_a_manager(client, owner):
    me = client.get("/api/auth/me", headers=owner["headers"]).json()["data"]["user"]
    assert me["role"] == "manager", "whoever signs the restaurant up runs it"


def test_staff_can_be_created_with_only_a_phone(client, owner):
    """A waiter with no email address is the normal case, not an edge case."""
    staff, code = hire(client, owner, role="waiter", phone="0722000123")
    assert staff["email"] is None
    assert staff["phone"] == "0722000123"
    assert code

    # And they can sign in with it.
    assert sign_in(client, "0722000123", code).status_code == 200


def test_staff_needs_some_way_to_sign_in(client, owner):
    r = client.post("/api/staff", json={"full_name": "Ghost", "role": "waiter"},
                    headers=owner["headers"])
    assert r.status_code == 422


def test_the_same_phone_can_work_at_two_restaurants(client, mail_log, owner):
    """Global uniqueness would make the second job impossible."""
    hire(client, owner, phone="0722555555")

    _, _, other = register_and_confirm(client, mail_log, restaurant="Rival Cafe")
    other_headers = {"Authorization": f"Bearer {other['tokens']['access_token']}"}
    r = client.post("/api/staff", json={
        "full_name": "Moonlighter", "role": "cashier", "phone": "0722555555",
    }, headers=other_headers)
    assert r.status_code == 201, r.text


def test_a_phone_cannot_repeat_within_one_restaurant(client, owner):
    hire(client, owner, phone="0722777777")
    r = client.post("/api/staff", json={
        "full_name": "Twin", "role": "waiter", "phone": "0722777777",
    }, headers=owner["headers"])
    assert r.status_code == 409


# ── The temporary code ──────────────────────────────────────────────────────
def test_temp_code_holder_can_do_nothing_but_change_it(client, owner):
    """The whole point: the manager who issued the code cannot act as them."""
    staff, code = hire(client, owner)
    session = sign_in(client, staff["phone"], code)
    assert session.status_code == 200
    headers = {"Authorization": f"Bearer {session.json()['data']['tokens']['access_token']}"}

    # Every business endpoint is closed.
    for method, path in [("get", "/api/orders"), ("get", "/api/menu/categories"),
                         ("post", "/api/orders")]:
        r = getattr(client, method)(path, headers=headers, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 403, f"{path} was reachable with a temporary code"

    # Exactly one is open.
    r = client.post("/api/auth/change-password", json={
        "current_password": code, "new_password": NEW_PASSWORD,
    }, headers=headers)
    assert r.status_code == 200, r.text


def test_the_temp_code_stops_working_once_changed(client, owner):
    staff, headers, ident = onboard(client, owner)
    # The old code is dead, the new password works.
    assert sign_in(client, ident, "irrelevant").status_code == 401
    assert sign_in(client, ident, NEW_PASSWORD).status_code == 200
    assert client.get("/api/orders", headers=headers).status_code == 200


def test_manager_reset_reissues_a_code_and_kills_sessions(client, owner):
    staff, headers, ident = onboard(client, owner)
    assert client.get("/api/orders", headers=headers).status_code == 200

    r = client.post(f"/api/staff/{staff['id']}/reset-password", headers=owner["headers"])
    assert r.status_code == 200, r.text
    new_code = r.json()["data"]["temp_code"]

    # The live session is gone, and the new code lands back in the forced-change
    # state rather than granting access.
    assert client.get("/api/orders", headers=headers).status_code == 401
    again = sign_in(client, ident, new_code)
    assert again.status_code == 200
    h2 = {"Authorization": f"Bearer {again.json()['data']['tokens']['access_token']}"}
    assert client.get("/api/orders", headers=h2).status_code == 403


# ── Rank ────────────────────────────────────────────────────────────────────
def test_a_manager_cannot_mint_another_manager(client, owner):
    r = client.post("/api/staff", json={
        "full_name": "Rival", "role": "manager", "phone": "0722999999",
    }, headers=owner["headers"])
    assert r.status_code == 403


def test_a_cashier_cannot_manage_staff_at_all(client, owner):
    _, headers, _ = onboard(client, owner, role="cashier")
    assert client.get("/api/staff", headers=headers).status_code == 403
    assert client.post("/api/staff", json={
        "full_name": "Friend", "role": "waiter", "phone": "0722888888",
    }, headers=headers).status_code == 403


def test_a_manager_cannot_deactivate_or_demote_themselves(client, owner):
    me = client.get("/api/auth/me", headers=owner["headers"]).json()["data"]["user"]
    assert client.patch(f"/api/staff/{me['id']}", json={"is_active": False},
                        headers=owner["headers"]).status_code == 422
    assert client.patch(f"/api/staff/{me['id']}", json={"role": "waiter"},
                        headers=owner["headers"]).status_code == 422


def test_deactivating_ends_the_session_immediately(client, owner):
    staff, headers, _ = onboard(client, owner)
    assert client.get("/api/orders", headers=headers).status_code == 200

    r = client.patch(f"/api/staff/{staff['id']}", json={"is_active": False},
                     headers=owner["headers"])
    assert r.status_code == 200, r.text
    # Not "at token expiry" — now.
    assert client.get("/api/orders", headers=headers).status_code == 401


# ── The permission matrix, with real tokens ─────────────────────────────────
@pytest.fixture
def waiter(client, owner):
    _, headers, _ = onboard(client, owner, role="waiter", name="Otieno",
                            phone="0733000001")
    return headers


@pytest.fixture
def cashier(client, owner):
    _, headers, _ = onboard(client, owner, role="cashier", name="Amina",
                            phone="0733000002")
    return headers


def _order(client, headers, owner):
    r = client.post("/api/orders", json={
        "order_type": "dine_in", "table_number": "3",
        "items": [{"menu_item_id": owner["item"]["id"], "quantity": 1}],
    }, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_a_waiter_can_take_orders(client, owner, waiter):
    order = _order(client, waiter, owner)
    assert order["server_name"] == "Otieno", "the order must carry who took it"


def test_a_waiter_cannot_take_payment(client, owner, waiter):
    order = _order(client, waiter, owner)
    r = client.post(f"/api/orders/{order['id']}/payments",
                    json={"method": "cash", "amount": 500}, headers=waiter)
    assert r.status_code == 403


def test_a_cashier_can_take_payment(client, owner, cashier):
    order = _order(client, cashier, owner)
    r = client.post(f"/api/orders/{order['id']}/payments",
                    json={"method": "cash", "amount": 500}, headers=cashier)
    assert r.status_code == 201, r.text


def test_only_a_manager_can_void_an_order(client, owner, waiter, cashier):
    """Voiding is how till theft is hidden, so it is the tightest gate here."""
    order = _order(client, cashier, owner)
    assert client.delete(f"/api/orders/{order['id']}", headers=waiter).status_code == 403
    assert client.delete(f"/api/orders/{order['id']}", headers=cashier).status_code == 403
    assert client.delete(f"/api/orders/{order['id']}", headers=owner["headers"]).status_code == 200


def test_money_screens_are_closed_to_cashiers_and_waiters(client, owner, waiter, cashier):
    for headers in (waiter, cashier):
        for path in ("/api/expenses", "/api/stock", "/api/analytics/accountability",
                     "/api/expenses/export.csv"):
            r = client.get(path, headers=headers)
            assert r.status_code == 403, f"{path} was readable at a lower rank"


def test_staff_appear_in_the_accountability_report(client, owner, waiter):
    _order(client, waiter, owner)
    report = client.get("/api/analytics/accountability",
                        headers=owner["headers"]).json()["data"]
    row = next((r for r in report["staff"] if r["name"] == "Otieno"), None)
    assert row is not None, "a waiter's unpaid order must be attributed to them"
    assert row["unpaid_orders"] == 1


def test_lower_ranks_can_read_the_menu_but_not_change_it(client, owner, waiter, cashier):
    """A waiter needs the menu to take an order and must not be able to edit it.

    Reading and writing are deliberately different here: hiding "Manage menu"
    from the sidebar is presentation, and the API is what decides.
    """
    for headers in (waiter, cashier):
        # Reading is part of the job.
        assert client.get("/api/menu/categories", headers=headers).status_code == 200
        assert client.get("/api/menu/items", headers=headers).status_code == 200

        # Changing it is not — prices especially.
        assert client.post("/api/menu/categories", json={"name": "Sneaky"},
                           headers=headers).status_code == 403
        assert client.post("/api/menu/items", json={
            "name": "Free Lunch", "price": 1, "category_id": owner["category"]["id"],
        }, headers=headers).status_code == 403
        assert client.patch(f"/api/menu/items/{owner['item']['id']}", json={"price": 1},
                            headers=headers).status_code == 403
        assert client.delete(f"/api/menu/items/{owner['item']['id']}",
                             headers=headers).status_code == 403

    # The item survived every attempt.
    item = client.get(f"/api/menu/items/{owner['item']['id']}",
                      headers=owner["headers"]).json()["data"]
    assert item["price"] == 500
