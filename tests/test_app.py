"""End to end against a real Postgres. Needs TEST_DATABASE_URL pointing at an
empty database — run_tests.sh makes a fresh one each time."""
import os
import re
import sys

import pytest

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
os.environ.setdefault("ADMIN_PIN", "246810")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("RATE_LIMIT", "1000")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as gs  # noqa: E402


@pytest.fixture(scope="module")
def staff():
    c = gs.app.test_client()
    r = c.post("/signin", data={"who": "Moiz", "pin": "246810"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin")
    return c


@pytest.fixture(scope="module")
def world(staff):
    r = staff.post("/admin/api/products", json={
        "name": "Ethiopia Yirgacheffe", "category": "Coffee beans", "unit": "kg",
        "description": "Floral, citrus",
        "variants": [{"label": "Raw", "price": 900},
                     {"label": "Roasted – Morning Light", "price": 1400},
                     {"label": "Roasted – Dark Knight", "price": 1450}]})
    assert r.status_code == 201, r.json
    eth = r.json["id"]
    r = staff.post("/admin/api/products", json={
        "name": "8oz ripple cup", "category": "Cups & lids", "unit": "box of 1000",
        "min_qty": 2, "variants": [{"label": "Kraft", "price": 2600}]})
    cups = r.json["id"]
    r = staff.post("/admin/api/cafes", json={"name": "Brew Lab", "phone": "98250 11111",
                                             "pin": "1234", "address": "Ghod Dod Rd",
                                             "pincode": "395007"})
    assert r.status_code == 201, r.json
    assert "wa.me/919825011111" in r.json["invite"]
    a = r.json["id"]
    r = staff.post("/admin/api/cafes", json={"name": "Second Cup", "phone": "9825022222",
                                             "pin": "5678"})
    b = r.json["id"]
    return {"eth": eth, "cups": cups, "a": a, "b": b}


def cafe_client(phone, pin):
    c = gs.app.test_client()
    r = c.post("/cafe/signin", data={"phone": phone, "pin": pin})
    assert r.status_code == 302
    return c


def test_no_public_shop():
    c = gs.app.test_client()
    r = c.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/cafe")
    assert c.get("/cafe").status_code == 200
    assert c.get("/admin").status_code == 302
    assert c.get("/admin/api/orders").status_code == 401
    assert c.get("/cafe/api/menu").status_code == 401
    for gone in ("/kitchen", "/packing", "/delivery", "/party", "/api/menu"):
        assert c.get(gone).status_code == 404, gone


def test_bad_pin_refused():
    c = gs.app.test_client()
    r = c.post("/signin", data={"who": "Moiz", "pin": "000000"})
    assert r.status_code == 200 and b"don&#39;t match" in r.data


def test_logo_served():
    r = gs.app.test_client().get("/asset/logo")
    assert r.status_code == 200 and r.mimetype == "image/png"


def test_price_sheet_and_catalogue(staff, world):
    eth, a = world["eth"], world["a"]
    r = staff.post(f"/admin/api/cafes/{a}/sheet", json={
        "prices": {f"{eth}|roasted-morning-light": 1300},
        "hidden": [f"{eth}|raw"]})
    assert r.json == {"ok": True, "custom": 1, "hidden": 1}
    c = cafe_client("9825011111", "1234")
    m = c.get("/cafe/api/menu").json
    ethp = next(p for p in m["products"] if p["id"] == eth)
    labels = {v["label"]: v["price"] for v in ethp["variants"]}
    assert "Raw" not in labels
    assert labels["Roasted – Morning Light"] == 1300
    assert labels["Roasted – Dark Knight"] == 1450
    # the other cafe still sees list prices and Raw
    c2 = cafe_client("9825022222", "5678")
    ethp2 = next(p for p in c2.get("/cafe/api/menu").json["products"] if p["id"] == eth)
    assert {v["label"]: v["price"] for v in ethp2["variants"]}["Raw"] == 900


def test_order_priced_server_side_and_min_qty(staff, world):
    eth, cups = world["eth"], world["cups"]
    c = cafe_client("9825011111", "1234")
    r = c.post("/cafe/api/orders", json={"items": [
        {"product_id": cups, "variant": "kraft", "qty": 1}]})
    assert r.status_code == 400 and "at least 2" in r.json["error"]
    r = c.post("/cafe/api/orders", json={"items": [
        {"product_id": eth, "variant": "raw", "qty": 5}]})
    assert r.status_code == 400  # hidden from this cafe
    r = c.post("/cafe/api/orders", json={"items": [
        {"product_id": eth, "variant": "roasted-morning-light", "qty": 10, "price": 1},
        {"product_id": cups, "variant": "kraft", "qty": 2}],
        "fulfilment": "porter", "notes": "Back gate"})
    assert r.status_code == 201, r.json
    assert r.json["subtotal"] == 10 * 1300 + 2 * 2600
    assert r.json["code"].startswith("GS")
    # past date refused
    r = c.post("/cafe/api/orders", json={"items": [
        {"product_id": eth, "variant": "roasted-dark-knight", "qty": 1}],
        "needed_by": "2001-01-01"})
    assert r.status_code == 400


def _order(staff, code):
    os_ = staff.get(f"/admin/api/orders?view=all&q={code}").json["orders"]
    return next(o for o in os_ if o["code"] == code)


def test_porter_flow_and_payments(staff, world):
    eth = world["eth"]
    c = cafe_client("9825011111", "1234")
    r = c.post("/cafe/api/orders", json={"items": [
        {"product_id": eth, "variant": "roasted-dark-knight", "qty": 4}]})
    code = r.json["code"]
    o = _order(staff, code)
    assert o["status"] == "new" and o["fare_pending"] and o["due"] == 5800
    # fare added to the bill
    r = staff.post(f"/admin/api/orders/{o['id']}/porter", json={"fare": 240, "ref": "CRN 8812"})
    assert r.json["order"]["total"] == 6040 and not r.json["order"]["fare_pending"]
    r = staff.post(f"/admin/api/orders/{o['id']}/status", json={"status": "ready"})
    assert r.json["order"]["status_label"] == "Ready for pickup"
    assert "wa.me/919825011111" in r.json["whatsapp"]
    r = staff.post(f"/admin/api/orders/{o['id']}/status", json={"status": "dispatched"})
    assert r.json["order"]["status_label"] == "Picked up"
    # bill page shows fare and a QR once a UPI id exists
    staff.post("/admin/api/settings", json={"upi_id": "goodstuff@okaxis", "whatsapp": "+91 98989 89898"})
    page = gs.app.test_client().get("/o/" + o["url"].rsplit("/o/", 1)[1])
    assert page.status_code == 200
    assert b"6,040" in page.data and b"<svg" in page.data and b"CRN 8812" not in page.data
    # wrong token is a plain 404
    assert gs.app.test_client().get(f"/o/{code}-aaaaaaaa").status_code == 404

    # money owed covers both of Brew Lab's orders; pay FIFO
    dues = staff.get("/admin/api/dues").json
    g = next(x for x in dues["cafes"] if x["cafe_id"] == world["a"])
    owed = g["owed"]
    assert owed == 18200 + 6040
    r = staff.post(f"/admin/api/cafes/{world['a']}/payments", json={"amount": owed + 1})
    assert r.status_code == 400
    r = staff.post(f"/admin/api/cafes/{world['a']}/payments",
                   json={"amount": 20000, "method": "bank", "ref": "UTR1"})
    assert r.status_code == 200
    assert r.json["alloc"][0]["amount"] == 18200          # oldest first
    assert r.json["alloc"][1] == {"order_id": o["id"], "code": code, "amount": 1800}
    assert r.json["left_owed"] == owed - 20000
    o = _order(staff, code)
    assert o["pay_state"] == "part" and o["due"] == 4240
    # cancelling a part-paid order is refused
    r = staff.post(f"/admin/api/orders/{o['id']}/status", json={"status": "cancelled"})
    assert r.status_code == 400
    # undo puts it back exactly
    r = staff.delete(f"/admin/api/payments/{_last_payment(staff)}")
    assert r.status_code == 200
    assert _order(staff, code)["paid_amount"] == 0
    # cafe sees what it owes and a statement link
    m = c.get("/cafe/api/menu").json["cafe"]
    assert m["owed"] == owed and "/s/" in m["statement"]
    st = gs.app.test_client().get("/s/" + m["statement"].rsplit("/s/", 1)[1])
    assert st.status_code == 200 and "24,240".encode() in st.data
    assert gs.app.test_client().get(f"/s/{world['a']}-zzzzzzzz").status_code == 404
    # whole-cafe bill on WhatsApp
    b = staff.get(f"/admin/api/cafes/{world['a']}/bill").json
    assert "Total due across 2 orders" in b["text"] and "goodstuff@okaxis" in b["text"]


def _last_payment(staff):
    return staff.get("/admin/api/dues").json["payments"][0]["id"]


def test_cafe_cancel_and_edit(staff, world):
    eth = world["eth"]
    c = cafe_client("9825022222", "5678")
    r = c.post("/cafe/api/orders", json={"items": [{"product_id": eth, "variant": "raw", "qty": 30}],
                                         "fulfilment": "collect"})
    oid = r.json["id"]
    assert r.json["total"] == 27000
    # admin edits items while new, re-priced from sheet
    r = staff.post(f"/admin/api/orders/{oid}/edit", json={"items": [
        {"product_id": eth, "variant": "raw", "qty": 20}], "notes": "half sack"})
    assert r.json["order"]["subtotal"] == 18000 and r.json["order"]["total"] == 18000
    # switch to porter, add fare, then back to collect clears it
    staff.post(f"/admin/api/orders/{oid}/edit", json={"fulfilment": "porter"})
    staff.post(f"/admin/api/orders/{oid}/porter", json={"fare": 300})
    r = staff.post(f"/admin/api/orders/{oid}/edit", json={"fulfilment": "collect"})
    assert r.json["order"]["total"] == 18000 and r.json["order"]["porter_fare"] == 0
    assert c.post(f"/cafe/api/orders/{oid}/cancel").json == {"ok": True}
    # cannot cancel another cafe's order
    other = cafe_client("9825011111", "1234")
    oid2 = other.post("/cafe/api/orders", json={"items": [
        {"product_id": eth, "variant": "roasted-dark-knight", "qty": 1}]}).json["id"]
    assert c.post(f"/cafe/api/orders/{oid2}/cancel").status_code == 400


def test_new_order_for_cafe_from_admin(staff, world):
    r = staff.post("/admin/api/orders", json={"cafe_id": world["b"], "items": [
        {"product_id": world["cups"], "variant": "kraft", "qty": 3}], "needed_by": ""})
    assert r.status_code == 201 and r.json["subtotal"] == 7800
    o = _order(staff, r.json["code"])
    assert o["taken_by"] == "Moiz"


def test_switched_off_cafe_cannot_sign_in(staff, world):
    staff.post(f"/admin/api/cafes/{world['b']}", json={"active": False})
    c = gs.app.test_client()
    r = c.post("/cafe/signin", data={"phone": "9825022222", "pin": "5678"})
    assert "e=" in r.headers["Location"]
    staff.post(f"/admin/api/cafes/{world['b']}", json={"active": True})


def test_people(staff):
    r = staff.post("/admin/api/users", json={"name": "Huzefa", "pin": "4321"})
    assert r.status_code == 201
    c = gs.app.test_client()
    assert c.post("/signin", data={"who": "huzefa", "pin": "4321"}).status_code == 302
    users = staff.get("/admin/api/settings").json["users"]
    moiz = next(u for u in users if u["name"] == "Moiz")
    hz = next(u for u in users if u["name"] == "Huzefa")
    staff.post(f"/admin/api/users/{hz['id']}", json={"active": False})
    r = staff.post(f"/admin/api/users/{moiz['id']}", json={"active": False})
    assert r.status_code == 400  # would leave nobody


def test_settings_mask_token(staff):
    staff.post("/admin/api/settings", json={"telegram_token": "123456:ABCDEFGHIJKLMNOP"})
    s = staff.get("/admin/api/settings").json["settings"]
    assert s["telegram_token"].startswith("123456") and "…" in s["telegram_token"]
    staff.post("/admin/api/settings", json={"telegram_token": s["telegram_token"]})
    with gs.db_cursor() as cur:
        assert gs.get_settings(cur)["telegram_token"] == "123456:ABCDEFGHIJKLMNOP"
    staff.post("/admin/api/settings", json={"telegram_token": ""})


def test_export_and_pages(staff):
    r = staff.get("/admin/export.csv")
    assert r.status_code == 200 and b"Ethiopia Yirgacheffe" in r.data
    assert staff.get("/admin").status_code == 200


def test_schema_is_its_own(staff):
    with gs.db_cursor() as cur:
        cur.execute("SELECT current_schema() AS s")
        assert cur.fetchone()["s"] == gs.DB_SCHEMA


def test_rupees():
    assert gs.rupees(1234567) == "₹12,34,567"
    assert gs.rupees(999) == "₹999"
