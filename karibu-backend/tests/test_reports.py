"""PDF and CSV exports.

The interesting assertions are not "did a file come back" — they are that the
file is the right TYPE, is SCOPED to the caller's restaurant, respects the ROLE
gate, and does not fall over on the text a real menu contains.
"""
from tests.conftest import register_and_confirm

PDF_MAGIC = b"%PDF-"


def _auth(session):
    return {"Authorization": f"Bearer {session['tokens']['access_token']}"}


def test_sales_pdf_is_a_pdf(client, owner):
    r = client.get("/api/analytics/sales.pdf?days=30", headers=owner["headers"])
    assert r.status_code == 200, r.text
    assert r.content.startswith(PDF_MAGIC)
    assert "application/pdf" in r.headers["content-type"]
    assert ".pdf" in r.headers["content-disposition"]


def test_sales_csv_is_a_csv(client, owner):
    r = client.get("/api/analytics/sales.csv?days=30", headers=owner["headers"])
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "Reference" in r.text


def test_expense_exports_render(client, owner):
    client.post("/api/expenses", json={
        "category": "stock", "amount": 4500, "payee": "Soko Market", "note": "Veg",
    }, headers=owner["headers"])

    pdf = client.get("/api/expenses/export.pdf?days=30", headers=owner["headers"])
    assert pdf.status_code == 200, pdf.text
    assert pdf.content.startswith(PDF_MAGIC)

    csv = client.get("/api/expenses/export.csv?days=30", headers=owner["headers"])
    assert csv.status_code == 200
    assert "Soko Market" in csv.text


def test_expense_csv_carries_a_utf8_bom(client, owner):
    """Without it Excel on Windows reads UTF-8 as Latin-1 and mangles names."""
    r = client.get("/api/expenses/export.csv?days=30", headers=owner["headers"])
    assert r.content.startswith(b"\xef\xbb\xbf")


def test_typographic_characters_do_not_break_the_pdf(client, owner):
    """fpdf's core fonts are Latin-1 and RAISE on anything else.

    A curly apostrophe from a phone keyboard in a payee name would have 500'd
    the whole report before the transliteration pass.
    """
    client.post("/api/expenses", json={
        "category": "stock",
        "amount": 1200,
        "payee": "Mama Njeri’s Kitchen — Stall 4",   # curly quote + em dash
        "note": "Order № 12 … delivered",             # numero + ellipsis
    }, headers=owner["headers"])

    r = client.get("/api/expenses/export.pdf?days=30", headers=owner["headers"])
    assert r.status_code == 200, r.text
    assert r.content.startswith(PDF_MAGIC)


def test_exports_are_scoped_to_the_callers_restaurant(client, mail_log, owner):
    """The sharpest failure mode for an export: another tenant's figures."""
    client.post("/api/expenses", json={
        "category": "rent", "amount": 90000, "payee": "SECRET-LANDLORD",
    }, headers=owner["headers"])

    _, _, other = register_and_confirm(client, mail_log, restaurant="Rival Cafe")
    r = client.get("/api/expenses/export.csv?days=30", headers=_auth(other))
    assert r.status_code == 200
    assert "SECRET-LANDLORD" not in r.text


def test_expense_exports_are_manager_only(client, owner):
    """Exports must not be a way round the gate on the screen.

    This used to introspect FastAPI's dependency tree, because the API could
    not create a non-manager to test with. It now signs in as a real cashier —
    see tests/test_staff.py, which covers the same ground for every money
    screen.
    """
    from tests.test_staff import onboard

    _, cashier, _ = onboard(client, owner, role="cashier", name="Till Person",
                            phone="0799000111")
    for path in ("/api/expenses/export.csv", "/api/expenses/export.pdf"):
        assert client.get(path, headers=cashier).status_code == 403, path

    # And the manager who owns the figures still gets them.
    assert client.get("/api/expenses/export.csv", headers=owner["headers"]).status_code == 200


def test_export_window_is_bounded(client, owner):
    """days is capped, or one request can ask the database for everything."""
    assert client.get("/api/analytics/sales.pdf?days=9999", headers=owner["headers"]).status_code == 422
    assert client.get("/api/expenses/export.csv?days=0", headers=owner["headers"]).status_code == 422
