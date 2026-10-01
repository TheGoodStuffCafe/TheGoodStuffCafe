"""
The Good Stuff — trade ordering for cafes.

One place, one team, one kind of customer: a cafe with an account.

    /cafe          cafes      sign in, order at their own prices, see what they owe
    /o/<slug>      anyone     one order's bill, with a UPI QR for what is still due
    /s/<slug>      anyone     a cafe's statement: every unpaid order, one QR
    /admin         staff      orders, money owed, cafes, products, settings
    /signin        staff      your own name, your own PIN

There is no public shop, no kitchen, no riders. Orders go out by Porter (we
book it and the fare is added to the bill) or the cafe collects. Everyone on
staff is a super admin.

Environment:
    DATABASE_URL   required — Postgres connection string
    DB_SCHEMA      optional — defaults to `goodstuff`; the schema this app owns
    ADMIN_PIN      optional — emergency login, and the PIN of the first account.
                              Remove it once everyone has their own account.
    ADMIN_NAME     optional — the first account's name (default Moiz)
    SECRET_KEY     set it — signs sessions and the private order links
"""

import csv
import functools
import hashlib
import hmac
import io
import json
import os
import random
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_SCHEMA = re.sub(r"[^a-z0-9_]", "", os.environ.get("DB_SCHEMA", "goodstuff").lower()) or "goodstuff"
ADMIN_PIN = os.environ.get("ADMIN_PIN", "").strip()
ADMIN_NAME = (os.environ.get("ADMIN_NAME", "").strip() or "Moiz")[:40]
IST = ZoneInfo("Asia/Kolkata")

MAX_UPLOAD = 6 * 1024 * 1024
ALLOWED_IMAGE = {"image/jpeg", "image/png", "image/webp"}
PHONE_RE = re.compile(r"^[6-9]\d{9}$")
PIN_RE = re.compile(r"^\d{4,8}$")
HERE = os.path.dirname(os.path.abspath(__file__))

# Where an order is. Porter orders and collected orders share the same three
# steps; only the words on the last one differ.
STATUSES = ["new", "ready", "dispatched", "cancelled"]
STATUS_LABEL = {"new": "Received", "ready": "Ready for pickup",
                "dispatched": "Picked up", "cancelled": "Cancelled"}
FULFILMENT = {"porter": "Porter", "collect": "Cafe collects"}
PAY_METHODS = {"upi": "UPI", "bank": "Bank transfer", "cash": "Cash", "other": "Other"}

# Suggestions for the category box. Anything typed is accepted; these are just
# what the shop is expected to carry, so the spelling stays consistent.
CATEGORY_HINTS = ["Coffee beans", "Cups & lids", "Desserts", "Syrups", "Dairy",
                  "Beverages", "Packaging", "Napkins & cutlery"]
UNIT_HINTS = ["kg", "box of 1000", "case of 24", "pack of 100", "litre", "piece"]

app = Flask(__name__, static_folder="static")
SECRET_KEY_SET = bool(os.environ.get("SECRET_KEY"))
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=60)
app.config.update(MAX_CONTENT_LENGTH=MAX_UPLOAD, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax")

# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

_pool = None
_pool_lock = threading.Lock()
_schema_lock = threading.Lock()
_schema_ready = False


def _dsn():
    url = DATABASE_URL
    if "sslmode" not in url and ".render.com" in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                # Every connection is pinned to this app's own schema, so an
                # unqualified table name can never land in another app's tables.
                _pool = ThreadedConnectionPool(
                    1, 8, _dsn(), options=f"-c search_path={DB_SCHEMA}")
    return _pool


class db_cursor:
    """Pooled cursor. Commits on clean exit, rolls back on exception."""

    def __init__(self, dict_rows=True):
        self.dict_rows = dict_rows

    def __enter__(self):
        self.conn = get_pool().getconn()
        factory = psycopg2.extras.RealDictCursor if self.dict_rows else None
        self.cur = self.conn.cursor(cursor_factory=factory)
        return self.cur

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.cur.close()
            get_pool().putconn(self.conn)


SCHEMA = """
CREATE TABLE IF NOT EXISTS gs_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS gs_assets (
    key        TEXT PRIMARY KEY,
    mime       TEXT        NOT NULL,
    data       BYTEA       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS gs_users (
    id         SERIAL PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    pin_hash   TEXT        NOT NULL,
    active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS gs_products (
    id          SERIAL PRIMARY KEY,
    name        TEXT        NOT NULL,
    category    TEXT        NOT NULL DEFAULT 'Coffee beans',
    description TEXT        NOT NULL DEFAULT '',
    unit        TEXT        NOT NULL DEFAULT 'kg',
    min_qty     INTEGER     NOT NULL DEFAULT 1,
    variants    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    sort        INTEGER     NOT NULL DEFAULT 0,
    active      BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS gs_cafes (
    id         SERIAL PRIMARY KEY,
    name       TEXT        NOT NULL,
    phone      TEXT UNIQUE NOT NULL,
    pin_hash   TEXT        NOT NULL,
    contact    TEXT        NOT NULL DEFAULT '',
    address    TEXT        NOT NULL DEFAULT '',
    pincode    TEXT        NOT NULL DEFAULT '',
    gstin      TEXT        NOT NULL DEFAULT '',
    notes      TEXT        NOT NULL DEFAULT '',
    prices     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    hidden     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS gs_orders (
    id            SERIAL PRIMARY KEY,
    code          TEXT UNIQUE NOT NULL,
    cafe_id       INTEGER     NOT NULL REFERENCES gs_cafes(id),
    cafe_name     TEXT        NOT NULL,
    phone         TEXT        NOT NULL DEFAULT '',
    address       TEXT        NOT NULL DEFAULT '',
    notes         TEXT        NOT NULL DEFAULT '',
    lines         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    subtotal      INTEGER     NOT NULL DEFAULT 0,
    porter_fare   INTEGER     NOT NULL DEFAULT 0,
    porter_ref    TEXT        NOT NULL DEFAULT '',
    total         INTEGER     NOT NULL DEFAULT 0,
    paid_amount   INTEGER     NOT NULL DEFAULT 0,
    written_off   BOOLEAN     NOT NULL DEFAULT FALSE,
    fulfilment    TEXT        NOT NULL DEFAULT 'porter',
    needed_by     DATE,
    status        TEXT        NOT NULL DEFAULT 'new',
    taken_by      TEXT        NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ready_at      TIMESTAMPTZ,
    dispatched_at TIMESTAMPTZ,
    cancelled_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS gs_orders_created_idx ON gs_orders (created_at DESC);
CREATE INDEX IF NOT EXISTS gs_orders_cafe_idx ON gs_orders (cafe_id, created_at);
CREATE INDEX IF NOT EXISTS gs_orders_status_idx ON gs_orders (status);

CREATE TABLE IF NOT EXISTS gs_payments (
    id         SERIAL PRIMARY KEY,
    cafe_id    INTEGER     NOT NULL REFERENCES gs_cafes(id),
    amount     INTEGER     NOT NULL,
    method     TEXT        NOT NULL DEFAULT 'upi',
    ref        TEXT        NOT NULL DEFAULT '',
    note       TEXT        NOT NULL DEFAULT '',
    alloc      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    by_name    TEXT        NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS gs_payments_cafe_idx ON gs_payments (cafe_id, created_at DESC);
"""

DEFAULT_SETTINGS = {
    "brand_name": "The Good Stuff",
    "whatsapp": "",          # the number cafes are told to message
    "upi_id": "",
    "pickup_address": "",    # where Porter and collecting cafes come to
    "portal_note": "",       # one line shown at the top of the cafe portal
    "telegram_token": "",
    "telegram_chat": "",
}


def hash_pin(pin, salt=None):
    """PINs are short, so the work factor does the heavy lifting."""
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${digest}"


def pin_matches(pin, stored):
    try:
        salt, _ = (stored or "").split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_pin(pin, salt), stored)


def _schema_fingerprint():
    return hashlib.sha256((SCHEMA + repr(sorted(DEFAULT_SETTINGS))).encode()).hexdigest()[:32]


_SCHEMA_LOCK_KEY = int.from_bytes(
    hashlib.sha256(f"goodstuff:{DB_SCHEMA}".encode()).digest()[:8], "big", signed=True)


def ensure_schema():
    """DDL runs only when the schema has actually changed, one worker at a
    time behind an advisory lock, so a deploy never races a live order."""
    global _schema_ready
    if _schema_ready:
        return
    conn = psycopg2.connect(_dsn())
    try:
        conn.autocommit = True
        with conn.cursor() as c:
            c.execute(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"')
    finally:
        conn.close()
    get_pool()
    want = _schema_fingerprint()
    with _schema_lock:
        if _schema_ready:
            return
        with db_cursor(dict_rows=False) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
            cur.execute("SELECT to_regclass('gs_settings')")
            if cur.fetchone()[0] is not None:
                cur.execute("SELECT value FROM gs_settings WHERE key = 'schema_fingerprint'")
                row = cur.fetchone()
                if row and row[0] == want:
                    _schema_ready = True
                    return
            cur.execute(SCHEMA)
            for k, v in DEFAULT_SETTINGS.items():
                cur.execute("INSERT INTO gs_settings (key, value) VALUES (%s, %s) "
                            "ON CONFLICT (key) DO NOTHING", (k, v))
            cur.execute("SELECT COUNT(*) FROM gs_users")
            if cur.fetchone()[0] == 0 and ADMIN_PIN:
                cur.execute("INSERT INTO gs_users (name, pin_hash) VALUES (%s, %s)",
                            (ADMIN_NAME, hash_pin(ADMIN_PIN)))
            cur.execute("""INSERT INTO gs_settings (key, value) VALUES ('schema_fingerprint', %s)
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (want,))
        _schema_ready = True


@app.before_request
def _boot():
    if request.endpoint in ("healthz", "static"):
        return
    ensure_schema()


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="That file is too big — keep it under 6 MB."), 413


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def now_ist():
    return datetime.now(IST)


def today():
    return now_ist().date()


def rupees(n):
    n = int(n or 0)
    s = str(abs(n))
    # Indian grouping: 1,23,456
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    return ("-" if n < 0 else "") + "₹" + s


def norm_phone(raw):
    digits = re.sub(r"\D", "", str(raw or ""))
    while digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return digits[:15]


def valid_phone(phone):
    return bool(PHONE_RE.match(phone or ""))


def whatsapp_link(phone, text):
    phone = norm_phone(phone)
    if not phone:
        return ""
    return f"https://wa.me/91{phone}?text=" + urllib.parse.quote(text)


def slugify(text, fallback="v"):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]
    return s or fallback


def int_or(v, fallback=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return fallback


def parse_date(raw):
    raw = (raw or "").strip()[:10]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError("That date isn't valid.")


def pretty_date(d):
    if not d:
        return ""
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except ValueError:
            return d
    t = today()
    if d == t:
        return "Today"
    if d == t + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%a %-d %b")


_recent = {}
_recent_lock = threading.Lock()
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "10"))


def rate_limited(key, window=60, limit=None):
    limit = RATE_LIMIT if limit is None else limit
    now = time.time()
    with _recent_lock:
        hits = [t for t in _recent.get(key, []) if now - t < window]
        limited = len(hits) >= limit
        if not limited:
            hits.append(now)
        _recent[key] = hits
        if len(_recent) > 2000:
            for k in [k for k, v in _recent.items() if not any(now - t < window for t in v)]:
                _recent.pop(k, None)
    return limited


def client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "?")


def get_settings(cur):
    cur.execute("SELECT key, value FROM gs_settings")
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in cur.fetchall()})
    return s


# --------------------------------------------------------------------------
# private links
#
# An order code is four digits; anybody who has one can count. Every public
# page is keyed on the code plus a token derived from the id and SECRET_KEY,
# so the links cannot be guessed and nothing needs storing.
# --------------------------------------------------------------------------

TOKEN_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def _token(kind, oid):
    key = app.secret_key
    mac = hmac.new(key.encode() if isinstance(key, str) else key,
                   f"gs-{kind}:{oid}".encode(), hashlib.sha256).digest()
    n = int.from_bytes(mac[:8], "big")
    out = ""
    for _ in range(8):
        out, n = out + TOKEN_ALPHABET[n % len(TOKEN_ALPHABET)], n // len(TOKEN_ALPHABET)
    return out


def order_slug(o):
    return f"{o['code']}-{_token('order', o['id'])}"


def cafe_slug(cafe_id):
    return f"{cafe_id}-{_token('cafe', cafe_id)}"


def base_url():
    return request.url_root.rstrip("/")


def order_url(o):
    return f"{base_url()}/o/{order_slug(o)}"


def statement_url(cafe_id):
    return f"{base_url()}/s/{cafe_slug(cafe_id)}"


# --------------------------------------------------------------------------
# UPI
# --------------------------------------------------------------------------

def upi_link(settings, amount=None, note="", ref=""):
    vpa = (settings.get("upi_id") or "").strip()
    if not vpa:
        return ""
    params = {"pa": vpa, "pn": (settings.get("brand_name") or "The Good Stuff")[:40], "cu": "INR"}
    if amount:
        params["am"] = f"{int(amount)}.00"
    if note:
        params["tn"] = note[:50]
    ref = re.sub(r"[^A-Za-z0-9]", "", ref or "")[:35]
    if ref:
        params["tr"] = ref
    return "upi://pay?" + urllib.parse.urlencode(params)


def qr_svg(payload, scale=5):
    import segno
    buf = io.BytesIO()
    segno.make(payload, error="m").save(buf, kind="svg", scale=scale, border=2,
                                        dark="#101010", light="#ffffff")
    return buf.getvalue().decode("utf-8")


# --------------------------------------------------------------------------
# alerts — Telegram, optional, and never allowed to lose an order
# --------------------------------------------------------------------------

def _esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(settings, text):
    token = (settings.get("telegram_token") or "").strip()
    chat = (settings.get("telegram_chat") or "").strip()
    if not token or not chat:
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def alert_async(settings, text):
    threading.Thread(target=send_telegram, args=(settings, text), daemon=True).start()


def order_alert_text(o):
    items = "\n".join(f"  {l['qty']} {_esc(l['unit'])} · {_esc(l['name'])} — {_esc(l['variant_label'])}"
                      for l in o["lines"][:20])
    when = f"Needed by {pretty_date(o['needed_by'])}" if o.get("needed_by") else "No date given"
    return (f"☕ <b>New order {o['code']}</b> · {_esc(o['cafe_name'])}\n{items}\n"
            f"{rupees(o['subtotal'])} + Porter · {FULFILMENT.get(o['fulfilment'], '')}\n{when}"
            + (f"\n📝 {_esc(o['notes'])}" if o.get("notes") else ""))


# --------------------------------------------------------------------------
# products
#
# A product is a thing on the list — "Ethiopia Yirgacheffe", "8oz ripple cup".
# Its variants are what a cafe actually picks, each with its own list price:
# Raw, Roasted – Morning Light, Roasted – Dark Knight. Everything is sold in
# whole units of the product's `unit`, which for beans is a kg.
# --------------------------------------------------------------------------

def clean_variants(raw):
    out, seen = [], set()
    for v in (raw or [])[:40]:
        if not isinstance(v, dict):
            continue
        label = (v.get("label") or "").strip()[:80]
        if not label:
            continue
        key = (v.get("key") or "").strip()[:40] or slugify(label)
        base, n = key, 2
        while key in seen:
            key, n = f"{base}-{n}", n + 1
        seen.add(key)
        price = max(0, min(10_000_000, int_or(v.get("price"))))
        out.append({"key": key, "label": label, "price": price,
                    "active": bool(v.get("active", True))})
    return out


def serialise_product(r):
    p = dict(r)
    p["variants"] = p.get("variants") or []
    if p.get("created_at"):
        p["created_at"] = p["created_at"].isoformat()
    return p


def get_products(cur, include_inactive=False):
    cur.execute("SELECT * FROM gs_products " + ("" if include_inactive else "WHERE active ")
                + "ORDER BY category, sort, name")
    return [serialise_product(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# cafes
# --------------------------------------------------------------------------

CAFE_COLUMNS = """id, name, phone, contact, address, pincode, gstin, notes, prices,
                  hidden, active, created_at, last_seen"""


def serialise_cafe(r):
    c = dict(r)
    for k in ("created_at", "last_seen"):
        if c.get(k):
            c[k] = c[k].isoformat()
    c["prices"] = c.get("prices") or {}
    c["hidden"] = c.get("hidden") or []
    c.pop("pin_hash", None)
    return c


def get_cafe(cur, cid, active_only=True):
    cur.execute(f"SELECT {CAFE_COLUMNS} FROM gs_cafes WHERE id = %s"
                + (" AND active" if active_only else ""), (cid,))
    row = cur.fetchone()
    return serialise_cafe(row) if row else None


def cafe_price(cafe, product, variant):
    """The cafe's own price if one is set, otherwise the list price. None if
    this cafe isn't offered it."""
    key = f"{product['id']}|{variant['key']}"
    if key in (cafe.get("hidden") or []):
        return None
    custom = int_or((cafe.get("prices") or {}).get(key))
    price = custom if custom > 0 else int(variant.get("price") or 0)
    return price if price > 0 else None


def cafe_catalogue(cur, cafe):
    """Only what this cafe can order, priced for this cafe."""
    out = []
    for p in get_products(cur):
        vs = []
        for v in p["variants"]:
            if not v.get("active", True):
                continue
            price = cafe_price(cafe, p, v)
            if price is None:
                continue
            vs.append({"key": v["key"], "label": v["label"], "price": price,
                       "list_price": int(v.get("price") or 0)})
        if vs:
            out.append({"id": p["id"], "name": p["name"], "category": p["category"],
                        "description": p["description"], "unit": p["unit"],
                        "min_qty": p["min_qty"], "variants": vs})
    return out


def build_lines(cur, cafe, raw_items):
    """Price an order from the database, never from the browser."""
    if not isinstance(raw_items, list):
        raise ValueError("Add at least one item.")
    products = {p["id"]: p for p in get_products(cur)}
    merged = {}
    for it in raw_items[:100]:
        if not isinstance(it, dict):
            continue
        pid = int_or(it.get("product_id"))
        vkey = str(it.get("variant") or "")
        qty = int_or(it.get("qty"))
        if qty <= 0:
            continue
        if qty > 100000:
            raise ValueError("That quantity is too large.")
        merged[(pid, vkey)] = merged.get((pid, vkey), 0) + qty
    lines, subtotal = [], 0
    for (pid, vkey), qty in merged.items():
        p = products.get(pid)
        if p is None:
            raise ValueError("Something in the order is no longer available. Refresh and try again.")
        v = next((x for x in p["variants"] if x["key"] == vkey and x.get("active", True)), None)
        price = cafe_price(cafe, p, v) if v else None
        if v is None or price is None:
            raise ValueError(f"{p['name']} — that option is no longer available.")
        if qty < max(1, int(p.get("min_qty") or 1)):
            raise ValueError(f"{p['name']} is sold in at least {p['min_qty']} {p['unit']}.")
        total = price * qty
        subtotal += total
        lines.append({"product_id": pid, "variant": vkey, "name": p["name"],
                      "variant_label": v["label"], "category": p["category"],
                      "unit": p["unit"], "qty": qty, "price": price, "line_total": total})
    if not lines:
        raise ValueError("Add at least one item.")
    lines.sort(key=lambda l: (l["category"], l["name"], l["variant_label"]))
    return lines, subtotal


# --------------------------------------------------------------------------
# orders
# --------------------------------------------------------------------------

def serialise_order(r, with_links=False):
    o = dict(r)
    for k in ("created_at", "ready_at", "dispatched_at", "cancelled_at"):
        if o.get(k):
            o[k] = o[k].isoformat()
    if o.get("needed_by"):
        o["needed_by"] = o["needed_by"].isoformat()
    o["lines"] = o.get("lines") or []
    o["due"] = 0 if (o["status"] == "cancelled" or o.get("written_off")) else \
        max(0, int(o["total"]) - int(o["paid_amount"] or 0))
    o["pay_state"] = pay_state(o)
    o["status_label"] = status_label(o)
    o["fare_pending"] = (o["fulfilment"] == "porter" and not o["porter_fare"]
                         and o["status"] != "cancelled")
    if with_links:
        o["url"] = order_url(o)
    return o


def pay_state(o):
    if o["status"] == "cancelled":
        return "cancelled"
    if o.get("written_off"):
        return "written_off"
    paid = int(o.get("paid_amount") or 0)
    if paid >= int(o["total"]) and int(o["total"]) > 0:
        return "paid"
    return "part" if paid > 0 else "unpaid"


def status_label(o):
    if o["status"] == "dispatched" and o.get("fulfilment") == "collect":
        return "Collected"
    return STATUS_LABEL.get(o["status"], o["status"])


def new_code(cur):
    for _ in range(30):
        code = "GS" + str(random.randint(1000, 9999))
        cur.execute("SELECT 1 FROM gs_orders WHERE code = %s", (code,))
        if cur.fetchone() is None:
            return code
    return "GS" + secrets.token_hex(3).upper()


def place_order(cafe_id, body, taken_by=""):
    """The one way an order is made. The cafe portal and the admin's 'New
    order' both come through here, so an order typed in for a cafe is exactly
    what the cafe would have placed: same prices, same bill."""
    fulfilment = body.get("fulfilment") if body.get("fulfilment") in FULFILMENT else "porter"
    try:
        needed_by = parse_date(body.get("needed_by"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if needed_by and needed_by < today():
        return jsonify(error="That date has already gone."), 400
    if needed_by and needed_by > today() + timedelta(days=120):
        return jsonify(error="Pick a date in the next four months."), 400
    notes = (body.get("notes") or "").strip()[:600]
    with db_cursor() as cur:
        cafe = get_cafe(cur, cafe_id)
        if cafe is None:
            return jsonify(error="That cafe isn't on the books, or is switched off."), 404
        try:
            lines, subtotal = build_lines(cur, cafe, body.get("items"))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        code = new_code(cur)
        address = cafe["address"] + (f" — {cafe['pincode']}" if cafe.get("pincode") else "")
        cur.execute(
            """INSERT INTO gs_orders (code, cafe_id, cafe_name, phone, address, notes, lines,
                                      subtotal, total, fulfilment, needed_by, taken_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s) RETURNING *""",
            (code, cafe["id"], cafe["name"], cafe["phone"], address[:700], notes,
             json.dumps(lines), subtotal, subtotal, fulfilment, needed_by, (taken_by or "")[:40]))
        order = serialise_order(cur.fetchone())
        settings = get_settings(cur)
    if not taken_by:
        alert_async(settings, order_alert_text(order))
    return jsonify(ok=True, code=code, id=order["id"], subtotal=subtotal,
                   total=order["total"], url=order_url(order)), 201


def cafe_owed(cur, cafe_id):
    cur.execute("""SELECT COALESCE(SUM(GREATEST(0, total - paid_amount)), 0) AS owed,
                          COUNT(*) FILTER (WHERE total > paid_amount) AS n
                     FROM gs_orders
                    WHERE cafe_id = %s AND status <> 'cancelled' AND NOT written_off""",
                (cafe_id,))
    r = cur.fetchone()
    return int(r["owed"] or 0), int(r["n"] or 0)


def unpaid_orders(cur, cafe_id, lock=False):
    cur.execute("""SELECT * FROM gs_orders
                    WHERE cafe_id = %s AND status <> 'cancelled' AND NOT written_off
                      AND total > paid_amount
                    ORDER BY created_at, id""" + (" FOR UPDATE" if lock else ""), (cafe_id,))
    return [serialise_order(r) for r in cur.fetchall()]


def bill_text(settings, cafe, orders):
    brand = settings.get("brand_name") or "The Good Stuff"
    parts = [f"*{brand}*", f"Statement for {cafe['name']}"]
    grand = 0
    for o in orders:
        parts.append("")
        head = f"Order {o['code']} — {o['created_at'][:10]}"
        parts.append(head)
        for l in o["lines"]:
            parts.append(f"{l['qty']} {l['unit']} {l['name']} ({l['variant_label']}) — {rupees(l['line_total'])}")
        if o["porter_fare"]:
            parts.append(f"Porter — {rupees(o['porter_fare'])}")
        elif o["fare_pending"]:
            parts.append("Porter — to be added")
        if o["paid_amount"]:
            parts.append(f"Paid already — {rupees(o['paid_amount'])}")
        parts.append(f"Due: {rupees(o['due'])}")
        grand += o["due"]
    if len(orders) > 1:
        parts += ["", f"*Total due across {len(orders)} orders: {rupees(grand)}*"]
    parts.append("")
    if settings.get("upi_id"):
        parts.append(f"UPI: {settings['upi_id']}")
    parts.append("Pay with the amount filled in: " + (
        order_url(orders[0]) if len(orders) == 1 else statement_url(cafe["id"])))
    return "\n".join(parts)


def status_text(settings, o):
    brand = settings.get("brand_name") or "The Good Stuff"
    if o["status"] == "ready":
        if o["fulfilment"] == "collect":
            msg = f"Your order {o['code']} is packed and ready to collect."
            if settings.get("pickup_address"):
                msg += f"\nPick up from: {settings['pickup_address']}"
        else:
            msg = f"Your order {o['code']} is packed and waiting for the Porter pickup."
    elif o["status"] == "dispatched":
        if o["fulfilment"] == "collect":
            msg = f"Your order {o['code']} has been collected. Thank you!"
        else:
            msg = f"Your order {o['code']} has been picked up by Porter and is on its way."
            if o.get("porter_ref"):
                msg += f"\nPorter: {o['porter_ref']}"
    elif o["status"] == "cancelled":
        msg = f"Your order {o['code']} has been cancelled."
    else:
        msg = f"We have your order {o['code']}."
    return f"*{brand}*\n{msg}\n\nBill: {order_url(o)}"


# --------------------------------------------------------------------------
# staff sign-in
# --------------------------------------------------------------------------

def staff_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("gs_staff"):
            if request.path.startswith("/admin/api") or request.is_json:
                return jsonify(error="signed_out"), 401
            return redirect(url_for("signin", next=request.path))
        return fn(*a, **kw)
    return wrapper


def who():
    return session.get("gs_staff", "") or "staff"


@app.route("/signin", methods=["GET", "POST"])
def signin():
    error = ""
    if request.method == "POST":
        name = (request.form.get("who") or "").strip()[:40]
        pin = (request.form.get("pin") or "").strip()
        if rate_limited("signin:" + client_ip()):
            error = "Too many tries. Wait a minute and try again."
        else:
            with db_cursor() as cur:
                cur.execute("SELECT id, name, pin_hash, active FROM gs_users "
                            "WHERE lower(name) = lower(%s)", (name,))
                row = cur.fetchone()
                ok = bool(row and row["active"] and pin and pin_matches(pin, row["pin_hash"]))
                if ok:
                    cur.execute("UPDATE gs_users SET last_seen = NOW() WHERE id = %s", (row["id"],))
                    name = row["name"]
                # The emergency door: any name with ADMIN_PIN. Close it by
                # removing ADMIN_PIN from the environment.
                elif ADMIN_PIN and name and hmac.compare_digest(pin, ADMIN_PIN):
                    ok = True
            if ok:
                session.permanent = True
                session["gs_staff"] = name
                nxt = request.args.get("next") or ""
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//")
                                else url_for("admin"))
            error = error or "That name and PIN don't match."
    with db_cursor() as cur:
        settings = get_settings(cur)
    return render_template("signin.html", error=error, settings=settings)


@app.route("/signout")
def signout():
    session.pop("gs_staff", None)
    return redirect(url_for("signin"))


# --------------------------------------------------------------------------
# public
# --------------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    try:
        ensure_schema()
        with db_cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
        return jsonify(ok=True, secret_key_set=SECRET_KEY_SET, schema=DB_SCHEMA)
    except Exception as e:
        return jsonify(ok=False, error=type(e).__name__), 503


@app.route("/")
def home():
    # No public shop. The root link is the cafe portal.
    return redirect(url_for("cafe_page"))


@app.route("/asset/logo")
def asset_logo():
    with db_cursor() as cur:
        cur.execute("SELECT mime, data FROM gs_assets WHERE key = 'logo'")
        row = cur.fetchone()
    if row:
        resp = Response(bytes(row["data"]), mimetype=row["mime"])
    else:
        resp = send_file(os.path.join(HERE, "static", "logo.png"), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route("/manifest.webmanifest")
def manifest():
    with db_cursor() as cur:
        s = get_settings(cur)
    return jsonify(name=s["brand_name"], short_name="Good Stuff", start_url="/cafe",
                   display="standalone", background_color="#120f0c", theme_color="#120f0c",
                   icons=[{"src": "/static/icon.png", "sizes": "512x512", "type": "image/png"}])


def _find_order_by_slug(cur, slug):
    code, _, token = (slug or "").rpartition("-")
    if not code or not token:
        return None
    cur.execute("SELECT * FROM gs_orders WHERE code = %s", (code.upper()[:20],))
    row = cur.fetchone()
    if row is None or not hmac.compare_digest(_token("order", row["id"]), token.lower()[:20]):
        return None
    return serialise_order(row)


@app.route("/o/<slug>")
def order_page(slug):
    with db_cursor() as cur:
        o = _find_order_by_slug(cur, slug)
        if o is None:
            abort(404)
        settings = get_settings(cur)
        cafe = get_cafe(cur, o["cafe_id"], active_only=False)
    upi = upi_link(settings, o["due"], f"{settings['brand_name']} {o['code']}", o["code"]) \
        if o["due"] > 0 else ""
    return render_template("bill.html", settings=settings, orders=[o], cafe=cafe,
                           total_due=o["due"], upi=upi, qr=qr_svg(upi) if upi else "",
                           single=True, rupees=rupees, pretty_date=pretty_date)


@app.route("/s/<slug>")
def statement_page(slug):
    cid_s, _, token = (slug or "").partition("-")
    cid = int_or(cid_s, -1)
    if cid < 0 or not hmac.compare_digest(_token("cafe", cid), (token or "").lower()[:20]):
        abort(404)
    with db_cursor() as cur:
        cafe = get_cafe(cur, cid, active_only=False)
        if cafe is None:
            abort(404)
        orders = unpaid_orders(cur, cid)
        settings = get_settings(cur)
    due = sum(o["due"] for o in orders)
    upi = upi_link(settings, due, f"{settings['brand_name']} statement", f"GSC{cid}") if due else ""
    return render_template("bill.html", settings=settings, orders=orders, cafe=cafe,
                           total_due=due, upi=upi, qr=qr_svg(upi) if upi else "",
                           single=False, rupees=rupees, pretty_date=pretty_date)


# --------------------------------------------------------------------------
# cafe portal
# --------------------------------------------------------------------------

def current_cafe(cur):
    cid = session.get("gs_cafe")
    return get_cafe(cur, cid) if cid else None


def cafe_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("gs_cafe"):
            return jsonify(error="signed_out"), 401
        return fn(*a, **kw)
    return wrapper


@app.route("/cafe")
def cafe_page():
    with db_cursor() as cur:
        cafe = current_cafe(cur)
        settings = get_settings(cur)
    return render_template("cafe.html", cafe=cafe, settings=settings,
                           error=request.args.get("e", ""))


@app.route("/cafe/signin", methods=["POST"])
def cafe_signin():
    phone = norm_phone(request.form.get("phone"))
    pin = (request.form.get("pin") or "").strip()
    if rate_limited("cafe:" + client_ip()):
        return redirect(url_for("cafe_page", e="Too many tries. Wait a minute and try again."))
    with db_cursor() as cur:
        cur.execute("SELECT id, pin_hash, active FROM gs_cafes WHERE phone = %s", (phone,))
        row = cur.fetchone()
        if row and row["active"] and pin and pin_matches(pin, row["pin_hash"]):
            session.permanent = True
            session["gs_cafe"] = row["id"]
            cur.execute("UPDATE gs_cafes SET last_seen = NOW() WHERE id = %s", (row["id"],))
            return redirect(url_for("cafe_page"))
    return redirect(url_for("cafe_page", e="That phone number and PIN don't match."))


@app.route("/cafe/signout")
def cafe_signout():
    session.pop("gs_cafe", None)
    return redirect(url_for("cafe_page"))


@app.route("/cafe/api/menu")
@cafe_required
def cafe_api_menu():
    with db_cursor() as cur:
        cafe = current_cafe(cur)
        if cafe is None:
            return jsonify(error="signed_out"), 401
        owed, n = cafe_owed(cur, cafe["id"])
        products = cafe_catalogue(cur, cafe)
    return jsonify(cafe={"name": cafe["name"], "address": cafe["address"],
                         "owed": owed, "unpaid_orders": n,
                         "statement": statement_url(cafe["id"]) if owed else ""},
                   products=products, today=today().isoformat())


@app.route("/cafe/api/orders", methods=["GET"])
@cafe_required
def cafe_api_orders():
    with db_cursor() as cur:
        cafe = current_cafe(cur)
        if cafe is None:
            return jsonify(error="signed_out"), 401
        cur.execute("SELECT * FROM gs_orders WHERE cafe_id = %s ORDER BY created_at DESC LIMIT 50",
                    (cafe["id"],))
        orders = [serialise_order(r, with_links=True) for r in cur.fetchall()]
        owed, n = cafe_owed(cur, cafe["id"])
    keep = ("id", "code", "created_at", "needed_by", "status", "status_label", "fulfilment",
            "subtotal", "porter_fare", "total", "paid_amount", "due", "pay_state",
            "fare_pending", "url", "lines", "notes")
    return jsonify(orders=[{k: o.get(k) for k in keep} for o in orders], owed=owed,
                   unpaid_orders=n)


@app.route("/cafe/api/orders", methods=["POST"])
@cafe_required
def cafe_place_order():
    if rate_limited("order:" + str(session.get("gs_cafe")), limit=20):
        return jsonify(error="Too many orders in a minute. Wait a moment."), 429
    return place_order(session["gs_cafe"], request.get_json(silent=True) or {})


@app.route("/cafe/api/orders/<int:oid>/cancel", methods=["POST"])
@cafe_required
def cafe_cancel_order(oid):
    """A cafe can take back an order nobody has packed yet."""
    with db_cursor() as cur:
        cur.execute("""UPDATE gs_orders SET status = 'cancelled', cancelled_at = NOW()
                        WHERE id = %s AND cafe_id = %s AND status = 'new' AND paid_amount = 0
                        RETURNING code, cafe_name""", (oid, session["gs_cafe"]))
        row = cur.fetchone()
        settings = get_settings(cur)
    if row is None:
        return jsonify(error="That order is already being packed — message us to change it."), 400
    alert_async(settings, f"✖️ <b>{row['code']}</b> cancelled by {_esc(row['cafe_name'])}")
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# admin
# --------------------------------------------------------------------------

@app.route("/admin")
@staff_required
def admin():
    with db_cursor() as cur:
        settings = get_settings(cur)
    return render_template("admin.html", settings=settings, me=who(),
                           category_hints=CATEGORY_HINTS, unit_hints=UNIT_HINTS,
                           secret_key_set=SECRET_KEY_SET, emergency_pin=bool(ADMIN_PIN))


@app.route("/admin/api/orders")
@staff_required
def admin_orders():
    view = request.args.get("view", "open")
    q = (request.args.get("q") or "").strip()[:60]
    where, vals = [], []
    if view == "open":
        where.append("status IN ('new', 'ready')")
    elif view == "done":
        where.append("status = 'dispatched'")
    elif view == "cancelled":
        where.append("status = 'cancelled'")
    if request.args.get("cafe"):
        where.append("cafe_id = %s"); vals.append(int_or(request.args.get("cafe")))
    if q:
        where.append("(code ILIKE %s OR cafe_name ILIKE %s)"); vals += [f"%{q}%", f"%{q}%"]
    sql = "SELECT * FROM gs_orders" + (" WHERE " + " AND ".join(where) if where else "")
    sql += (" ORDER BY (status = 'new') DESC, needed_by NULLS LAST, created_at"
            if view == "open" else " ORDER BY created_at DESC LIMIT 200")
    with db_cursor() as cur:
        cur.execute(sql, vals)
        orders = [serialise_order(r, with_links=True) for r in cur.fetchall()]
        cur.execute("""SELECT COUNT(*) FILTER (WHERE status = 'new') AS new,
                              COUNT(*) FILTER (WHERE status = 'ready') AS ready,
                              COALESCE(SUM(GREATEST(0, total - paid_amount))
                                FILTER (WHERE status <> 'cancelled' AND NOT written_off), 0) AS owed,
                              COUNT(*) FILTER (WHERE fulfilment = 'porter' AND porter_fare = 0
                                AND status IN ('new','ready','dispatched')) AS no_fare
                         FROM gs_orders""")
        counts = {k: int(v or 0) for k, v in cur.fetchone().items()}
        t0 = datetime.combine(today(), datetime.min.time(), IST)
        cur.execute("""SELECT COALESCE(SUM(subtotal), 0) AS sales, COUNT(*) AS n
                         FROM gs_orders WHERE status <> 'cancelled' AND created_at >= %s""",
                    (t0.replace(day=1),))
        m = cur.fetchone()
        counts["month_sales"], counts["month_orders"] = int(m["sales"]), int(m["n"])
    return jsonify(orders=orders, counts=counts, today=today().isoformat())


def _order_for_update(cur, oid):
    cur.execute("SELECT * FROM gs_orders WHERE id = %s FOR UPDATE", (oid,))
    row = cur.fetchone()
    return serialise_order(row) if row else None


@app.route("/admin/api/orders/<int:oid>/status", methods=["POST"])
@staff_required
def admin_order_status(oid):
    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status not in STATUSES:
        return jsonify(error="Unknown status."), 400
    with db_cursor() as cur:
        o = _order_for_update(cur, oid)
        if o is None:
            return jsonify(error="That order no longer exists."), 404
        if status == "cancelled" and o["paid_amount"] > 0:
            return jsonify(error="This order has payments against it. Undo the payment "
                                 "under Money owed first, then cancel."), 400
        stamp = {"ready": ", ready_at = COALESCE(ready_at, NOW())",
                 "dispatched": ", dispatched_at = NOW(), ready_at = COALESCE(ready_at, NOW())",
                 "cancelled": ", cancelled_at = NOW()"}.get(status, "")
        cur.execute(f"UPDATE gs_orders SET status = %s{stamp} WHERE id = %s RETURNING *",
                    (status, oid))
        o = serialise_order(cur.fetchone(), with_links=True)
        settings = get_settings(cur)
    return jsonify(ok=True, order=o,
                   whatsapp=whatsapp_link(o["phone"], status_text(settings, o)))


@app.route("/admin/api/orders/<int:oid>/porter", methods=["POST"])
@staff_required
def admin_porter(oid):
    """The Porter fare, once the trip is booked. It is added to the bill."""
    body = request.get_json(silent=True) or {}
    fare = int_or(body.get("fare"), -1)
    if fare < 0 or fare > 100000:
        return jsonify(error="The fare is a whole number of rupees."), 400
    ref = (body.get("ref") or "").strip()[:120]
    with db_cursor() as cur:
        o = _order_for_update(cur, oid)
        if o is None:
            return jsonify(error="That order no longer exists."), 404
        if o["status"] == "cancelled":
            return jsonify(error="This order is cancelled."), 400
        if o["paid_amount"] > o["subtotal"] + fare:
            return jsonify(error="They've already paid more than that would make the bill."), 400
        cur.execute("""UPDATE gs_orders SET porter_fare = %s, porter_ref = %s,
                              total = subtotal + %s, fulfilment = 'porter'
                        WHERE id = %s RETURNING *""", (fare, ref, fare, oid))
        o = serialise_order(cur.fetchone(), with_links=True)
    return jsonify(ok=True, order=o)


@app.route("/admin/api/orders/<int:oid>/edit", methods=["POST"])
@staff_required
def admin_edit_order(oid):
    """Change the items, date, notes or how it goes out. Items can only change
    while nothing is packed, and are re-priced from the cafe's sheet."""
    body = request.get_json(silent=True) or {}
    with db_cursor() as cur:
        o = _order_for_update(cur, oid)
        if o is None:
            return jsonify(error="That order no longer exists."), 404
        if o["status"] == "cancelled":
            return jsonify(error="This order is cancelled."), 400
        sets, vals = [], []
        if "items" in body:
            if o["status"] != "new":
                return jsonify(error="It's already packed. Move it back to Received to change items."), 400
            cafe = get_cafe(cur, o["cafe_id"], active_only=False)
            try:
                lines, subtotal = build_lines(cur, cafe, body["items"])
            except ValueError as e:
                return jsonify(error=str(e)), 400
            if o["paid_amount"] > subtotal + o["porter_fare"]:
                return jsonify(error="They've already paid more than the new total."), 400
            sets += ["lines = %s::jsonb", "subtotal = %s"]
            vals += [json.dumps(lines), subtotal]
        if "needed_by" in body:
            try:
                sets.append("needed_by = %s"); vals.append(parse_date(body["needed_by"]))
            except ValueError as e:
                return jsonify(error=str(e)), 400
        if "notes" in body:
            sets.append("notes = %s"); vals.append((body.get("notes") or "").strip()[:600])
        if body.get("fulfilment") in FULFILMENT:
            sets.append("fulfilment = %s"); vals.append(body["fulfilment"])
            if body["fulfilment"] == "collect":
                if o["paid_amount"] > o["subtotal"]:
                    return jsonify(error="They've already paid more than the bill without Porter."), 400
                sets += ["porter_fare = 0", "porter_ref = ''"]
        if not sets:
            return jsonify(error="Nothing to change."), 400
        cur.execute(f"UPDATE gs_orders SET {', '.join(sets)} WHERE id = %s", vals + [oid])
        # The total always follows from its two parts, whichever of them changed.
        cur.execute("UPDATE gs_orders SET total = subtotal + porter_fare WHERE id = %s RETURNING *",
                    (oid,))
        o = serialise_order(cur.fetchone(), with_links=True)
    return jsonify(ok=True, order=o)


@app.route("/admin/api/orders/<int:oid>/write-off", methods=["POST"])
@staff_required
def admin_write_off(oid):
    on = bool((request.get_json(silent=True) or {}).get("on", True))
    with db_cursor() as cur:
        cur.execute("UPDATE gs_orders SET written_off = %s WHERE id = %s RETURNING *", (on, oid))
        row = cur.fetchone()
    if row is None:
        return jsonify(error="That order no longer exists."), 404
    return jsonify(ok=True, order=serialise_order(row, with_links=True))


@app.route("/admin/api/orders", methods=["POST"])
@staff_required
def admin_new_order():
    """An order taken over the phone or WhatsApp, for a cafe."""
    body = request.get_json(silent=True) or {}
    return place_order(int_or(body.get("cafe_id")), body, taken_by=who())


@app.route("/admin/api/orders/<int:oid>/whatsapp")
@staff_required
def admin_order_whatsapp(oid):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM gs_orders WHERE id = %s", (oid,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="That order no longer exists."), 404
        o = serialise_order(row, with_links=True)
        settings = get_settings(cur)
        cafe = get_cafe(cur, o["cafe_id"], active_only=False)
    kind = request.args.get("kind", "status")
    text = bill_text(settings, cafe, [o]) if kind == "bill" else status_text(settings, o)
    return jsonify(text=text, link=whatsapp_link(o["phone"], text))


# ---- money owed and payments ----------------------------------------------

@app.route("/admin/api/dues")
@staff_required
def admin_dues():
    with db_cursor() as cur:
        cur.execute("""SELECT * FROM gs_orders
                        WHERE status <> 'cancelled' AND NOT written_off AND total > paid_amount
                        ORDER BY created_at""")
        orders = [serialise_order(r, with_links=True) for r in cur.fetchall()]
        cur.execute(f"SELECT {CAFE_COLUMNS} FROM gs_cafes")
        cafes = {r["id"]: serialise_cafe(r) for r in cur.fetchall()}
        cur.execute("""SELECT p.*, c.name AS cafe_name FROM gs_payments p
                         JOIN gs_cafes c ON c.id = p.cafe_id
                        ORDER BY p.created_at DESC LIMIT 40""")
        payments = []
        for r in cur.fetchall():
            r = dict(r); r["created_at"] = r["created_at"].isoformat()
            payments.append(r)
    groups = {}
    for o in orders:
        g = groups.setdefault(o["cafe_id"], {"cafe_id": o["cafe_id"], "orders": [], "owed": 0,
                                             "oldest": o["created_at"]})
        g["orders"].append(o)
        g["owed"] += o["due"]
    out = []
    for cid, g in groups.items():
        c = cafes.get(cid) or {}
        g.update(name=c.get("name", ""), phone=c.get("phone", ""),
                 statement=statement_url(cid),
                 days=(today() - date.fromisoformat(g["oldest"][:10])).days)
        out.append(g)
    out.sort(key=lambda g: -g["owed"])
    return jsonify(cafes=out, total=sum(g["owed"] for g in out), payments=payments)


@app.route("/admin/api/cafes/<int:cid>/bill")
@staff_required
def admin_cafe_bill(cid):
    with db_cursor() as cur:
        cafe = get_cafe(cur, cid, active_only=False)
        if cafe is None:
            return jsonify(error="That cafe no longer exists."), 404
        orders = unpaid_orders(cur, cid)
        settings = get_settings(cur)
    if not orders:
        return jsonify(error="Nothing owed."), 400
    text = bill_text(settings, cafe, orders)
    return jsonify(text=text, link=whatsapp_link(cafe["phone"], text))


@app.route("/admin/api/cafes/<int:cid>/payments", methods=["POST"])
@staff_required
def admin_record_payment(cid):
    """Money in from a cafe. Applied to the order named, then to their oldest
    unpaid orders, so one lump-sum transfer clears bills in the order they
    were raised."""
    body = request.get_json(silent=True) or {}
    amount = int_or(body.get("amount"))
    method = body.get("method") if body.get("method") in PAY_METHODS else "upi"
    if amount <= 0:
        return jsonify(error="Enter the amount received."), 400
    first = int_or(body.get("order_id"), 0)
    with db_cursor() as cur:
        orders = unpaid_orders(cur, cid, lock=True)
        owed = sum(o["due"] for o in orders)
        if not orders:
            return jsonify(error="This cafe doesn't owe anything."), 400
        if amount > owed:
            return jsonify(error=f"That's more than they owe ({rupees(owed)}). "
                                 "Enter at most what's due."), 400
        orders.sort(key=lambda o: (o["id"] != first,))
        left, alloc = amount, []
        for o in orders:
            if left <= 0:
                break
            take = min(left, o["due"])
            cur.execute("UPDATE gs_orders SET paid_amount = paid_amount + %s WHERE id = %s",
                        (take, o["id"]))
            alloc.append({"order_id": o["id"], "code": o["code"], "amount": take})
            left -= take
        cur.execute("""INSERT INTO gs_payments (cafe_id, amount, method, ref, note, alloc, by_name)
                       VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING id""",
                    (cid, amount, method, (body.get("ref") or "").strip()[:80],
                     (body.get("note") or "").strip()[:300], json.dumps(alloc), who()))
        pid = cur.fetchone()["id"]
    return jsonify(ok=True, id=pid, alloc=alloc, left_owed=owed - amount)


@app.route("/admin/api/payments/<int:pid>", methods=["DELETE"])
@staff_required
def admin_undo_payment(pid):
    """A payment entered by mistake comes off exactly the orders it went on."""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM gs_payments WHERE id = %s FOR UPDATE", (pid,))
        p = cur.fetchone()
        if p is None:
            return jsonify(error="That payment no longer exists."), 404
        for a in p["alloc"] or []:
            cur.execute("UPDATE gs_orders SET paid_amount = GREATEST(0, paid_amount - %s) "
                        "WHERE id = %s", (int(a["amount"]), int(a["order_id"])))
        cur.execute("DELETE FROM gs_payments WHERE id = %s", (pid,))
    return jsonify(ok=True)


# ---- cafes ----------------------------------------------------------------

def _cafe_fields(body):
    return {
        "name": (body.get("name") or "").strip()[:120],
        "contact": (body.get("contact") or "").strip()[:120],
        "address": (body.get("address") or "").strip()[:600],
        "pincode": re.sub(r"\D", "", str(body.get("pincode") or ""))[:6],
        "gstin": re.sub(r"[^A-Z0-9]", "", str(body.get("gstin") or "").upper())[:15],
        "notes": (body.get("notes") or "").strip()[:600],
    }


@app.route("/admin/api/cafes")
@staff_required
def admin_cafes():
    with db_cursor() as cur:
        cur.execute(f"""SELECT {CAFE_COLUMNS},
                          (SELECT COUNT(*) FROM gs_orders o WHERE o.cafe_id = c.id
                              AND o.status <> 'cancelled') AS orders,
                          (SELECT MAX(created_at) FROM gs_orders o WHERE o.cafe_id = c.id) AS last_order,
                          (SELECT COALESCE(SUM(GREATEST(0, total - paid_amount)), 0) FROM gs_orders o
                            WHERE o.cafe_id = c.id AND o.status <> 'cancelled'
                              AND NOT o.written_off) AS owed
                         FROM gs_cafes c ORDER BY active DESC, name""")
        cafes = []
        for r in cur.fetchall():
            c = serialise_cafe(r)
            c["last_order"] = r["last_order"].isoformat() if r["last_order"] else None
            c["owed"], c["orders"] = int(r["owed"]), int(r["orders"])
            c["custom_prices"] = len(c["prices"])
            cafes.append(c)
    return jsonify(cafes=cafes)


@app.route("/admin/api/cafes", methods=["POST"])
@staff_required
def admin_cafe_create():
    body = request.get_json(silent=True) or {}
    f = _cafe_fields(body)
    phone = norm_phone(body.get("phone"))
    pin = str(body.get("pin") or "").strip()
    if not f["name"]:
        return jsonify(error="Give the cafe a name."), 400
    if not valid_phone(phone):
        return jsonify(error="Enter a 10-digit mobile number — it's how they sign in."), 400
    if not PIN_RE.match(pin):
        return jsonify(error="Set a PIN of 4 to 8 digits."), 400
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM gs_cafes WHERE phone = %s", (phone,))
        if cur.fetchone():
            return jsonify(error="A cafe with that phone number already exists."), 400
        cur.execute("""INSERT INTO gs_cafes (name, phone, pin_hash, contact, address, pincode,
                                             gstin, notes)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (f["name"], phone, hash_pin(pin), f["contact"], f["address"], f["pincode"],
                     f["gstin"], f["notes"]))
        cid = cur.fetchone()["id"]
        settings = get_settings(cur)
    invite = (f"*{settings['brand_name']}* — your trade account is ready.\n"
              f"Order here: {base_url()}/cafe\nPhone: {phone}\nPIN: {pin}")
    return jsonify(ok=True, id=cid, invite=whatsapp_link(phone, invite)), 201


@app.route("/admin/api/cafes/<int:cid>", methods=["POST"])
@staff_required
def admin_cafe_update(cid):
    body = request.get_json(silent=True) or {}
    sets, vals = [], []
    if "name" in body:
        f = _cafe_fields(body)
        if not f["name"]:
            return jsonify(error="Give the cafe a name."), 400
        for k, v in f.items():
            sets.append(f"{k} = %s"); vals.append(v)
    if "phone" in body:
        phone = norm_phone(body.get("phone"))
        if not valid_phone(phone):
            return jsonify(error="Enter a 10-digit mobile number."), 400
        sets.append("phone = %s"); vals.append(phone)
    if body.get("pin"):
        pin = str(body["pin"]).strip()
        if not PIN_RE.match(pin):
            return jsonify(error="A PIN is 4 to 8 digits."), 400
        sets.append("pin_hash = %s"); vals.append(hash_pin(pin))
    if "active" in body:
        sets.append("active = %s"); vals.append(bool(body["active"]))
    if not sets:
        return jsonify(error="Nothing to change."), 400
    with db_cursor() as cur:
        try:
            cur.execute(f"UPDATE gs_cafes SET {', '.join(sets)} WHERE id = %s", vals + [cid])
        except psycopg2.IntegrityError:
            return jsonify(error="Another cafe already has that phone number."), 400
        if cur.rowcount != 1:
            return jsonify(error="That cafe no longer exists."), 404
    return jsonify(ok=True)


@app.route("/admin/api/cafes/<int:cid>/sheet")
@staff_required
def admin_cafe_sheet(cid):
    """Every product and option, its list price, and this cafe's price."""
    with db_cursor() as cur:
        cafe = get_cafe(cur, cid, active_only=False)
        if cafe is None:
            return jsonify(error="That cafe no longer exists."), 404
        products = get_products(cur)
    rows = []
    for p in products:
        for v in p["variants"]:
            if not v.get("active", True):
                continue
            key = f"{p['id']}|{v['key']}"
            rows.append({"key": key, "product": p["name"], "category": p["category"],
                         "variant": v["label"], "unit": p["unit"],
                         "list_price": int(v.get("price") or 0),
                         "price": int_or(cafe["prices"].get(key)) or None,
                         "hidden": key in cafe["hidden"]})
    return jsonify(cafe={"id": cafe["id"], "name": cafe["name"]}, rows=rows)


@app.route("/admin/api/cafes/<int:cid>/sheet", methods=["POST"])
@staff_required
def admin_cafe_sheet_save(cid):
    body = request.get_json(silent=True) or {}
    prices = {}
    for k, v in (body.get("prices") or {}).items():
        n = int_or(v)
        if isinstance(k, str) and "|" in k and 0 < n <= 10_000_000:
            prices[k[:90]] = n
    hidden = sorted({k[:90] for k in (body.get("hidden") or []) if isinstance(k, str) and "|" in k})
    with db_cursor() as cur:
        cur.execute("UPDATE gs_cafes SET prices = %s::jsonb, hidden = %s::jsonb WHERE id = %s",
                    (json.dumps(prices), json.dumps(hidden), cid))
        if cur.rowcount != 1:
            return jsonify(error="That cafe no longer exists."), 404
    return jsonify(ok=True, custom=len(prices), hidden=len(hidden))


@app.route("/admin/api/cafes/<int:cid>/sheet/copy", methods=["POST"])
@staff_required
def admin_cafe_sheet_copy(cid):
    src = int_or((request.get_json(silent=True) or {}).get("from"), -1)
    with db_cursor() as cur:
        cur.execute("SELECT prices, hidden FROM gs_cafes WHERE id = %s", (src,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error="Pick a cafe to copy from."), 400
        cur.execute("UPDATE gs_cafes SET prices = %s::jsonb, hidden = %s::jsonb WHERE id = %s",
                    (json.dumps(row["prices"] or {}), json.dumps(row["hidden"] or []), cid))
    return jsonify(ok=True)


@app.route("/admin/api/cafes/<int:cid>/catalogue")
@staff_required
def admin_cafe_catalogue(cid):
    """What the New order form can sell to this cafe — the same list its portal shows."""
    with db_cursor() as cur:
        cafe = get_cafe(cur, cid)
        if cafe is None:
            return jsonify(error="That cafe isn't on the books, or is switched off."), 404
        return jsonify(products=cafe_catalogue(cur, cafe))


@app.route("/admin/api/cafes/<int:cid>", methods=["DELETE"])
@staff_required
def admin_cafe_delete(cid):
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM gs_orders WHERE cafe_id = %s LIMIT 1", (cid,))
        if cur.fetchone():
            return jsonify(error="This cafe has orders. Switch it off instead, so the "
                                 "history keeps its name."), 400
        cur.execute("DELETE FROM gs_cafes WHERE id = %s", (cid,))
    return jsonify(ok=True)


# ---- products -------------------------------------------------------------

def _product_fields(body):
    return {
        "name": (body.get("name") or "").strip()[:120],
        "category": (body.get("category") or "").strip()[:60] or "Coffee beans",
        "description": (body.get("description") or "").strip()[:400],
        "unit": (body.get("unit") or "").strip()[:30] or "kg",
        "min_qty": max(1, min(100000, int_or(body.get("min_qty"), 1))),
        "sort": int_or(body.get("sort")),
        "active": bool(body.get("active", True)),
    }


@app.route("/admin/api/products")
@staff_required
def admin_products():
    with db_cursor() as cur:
        products = get_products(cur, include_inactive=True)
    return jsonify(products=products)


@app.route("/admin/api/products", methods=["POST"])
@staff_required
def admin_product_create():
    body = request.get_json(silent=True) or {}
    f = _product_fields(body)
    variants = clean_variants(body.get("variants"))
    if not f["name"]:
        return jsonify(error="Give it a name."), 400
    if not variants:
        return jsonify(error="Add at least one option, e.g. Raw or a roast name."), 400
    with db_cursor() as cur:
        cur.execute("""INSERT INTO gs_products (name, category, description, unit, min_qty,
                                                variants, sort, active)
                       VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING id""",
                    (f["name"], f["category"], f["description"], f["unit"], f["min_qty"],
                     json.dumps(variants), f["sort"], f["active"]))
        pid = cur.fetchone()["id"]
    return jsonify(ok=True, id=pid), 201


@app.route("/admin/api/products/<int:pid>", methods=["POST"])
@staff_required
def admin_product_update(pid):
    body = request.get_json(silent=True) or {}
    sets, vals = [], []
    if "name" in body:
        f = _product_fields(body)
        if not f["name"]:
            return jsonify(error="Give it a name."), 400
        for k, v in f.items():
            sets.append(f"{k} = %s"); vals.append(v)
    elif "active" in body:
        sets.append("active = %s"); vals.append(bool(body["active"]))
    if "variants" in body:
        variants = clean_variants(body.get("variants"))
        if not variants:
            return jsonify(error="Keep at least one option."), 400
        sets.append("variants = %s::jsonb"); vals.append(json.dumps(variants))
    if not sets:
        return jsonify(error="Nothing to change."), 400
    with db_cursor() as cur:
        cur.execute(f"UPDATE gs_products SET {', '.join(sets)} WHERE id = %s", vals + [pid])
        if cur.rowcount != 1:
            return jsonify(error="That product no longer exists."), 404
    return jsonify(ok=True)


@app.route("/admin/api/products/<int:pid>", methods=["DELETE"])
@staff_required
def admin_product_delete(pid):
    # Old orders carry their own copy of name and price, so deleting is safe.
    with db_cursor() as cur:
        cur.execute("DELETE FROM gs_products WHERE id = %s", (pid,))
    return jsonify(ok=True)


# ---- settings, logo, people -------------------------------------------------

@app.route("/admin/api/settings")
@staff_required
def admin_settings_get():
    with db_cursor() as cur:
        s = get_settings(cur)
        cur.execute("SELECT updated_at FROM gs_assets WHERE key = 'logo'")
        logo = cur.fetchone()
        cur.execute("SELECT id, name, active, created_at, last_seen FROM gs_users ORDER BY name")
        users = []
        for r in cur.fetchall():
            u = dict(r)
            for k in ("created_at", "last_seen"):
                u[k] = u[k].isoformat() if u.get(k) else None
            users.append(u)
    s.pop("schema_fingerprint", None)
    tok = s.get("telegram_token") or ""
    s["telegram_token"] = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else ("set" if tok else "")
    return jsonify(settings=s, custom_logo=bool(logo), users=users, me=who())


@app.route("/admin/api/settings", methods=["POST"])
@staff_required
def admin_settings_save():
    body = request.get_json(silent=True) or {}
    clean = {}
    for k in DEFAULT_SETTINGS:
        if k not in body:
            continue
        v = str(body.get(k) or "").strip()[:400]
        if k == "telegram_token" and ("…" in v or v == "set"):
            continue   # the masked value came back unchanged
        if k == "whatsapp":
            v = norm_phone(v)
        if k == "brand_name" and not v:
            v = DEFAULT_SETTINGS["brand_name"]
        clean[k] = v
    with db_cursor() as cur:
        for k, v in clean.items():
            cur.execute("""INSERT INTO gs_settings (key, value) VALUES (%s, %s)
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (k, v))
    return jsonify(ok=True)


@app.route("/admin/api/settings/telegram-test", methods=["POST"])
@staff_required
def admin_telegram_test():
    with db_cursor() as cur:
        s = get_settings(cur)
    ok = send_telegram(s, f"✅ {_esc(s['brand_name'])} alerts are working.")
    return (jsonify(ok=True) if ok else
            jsonify(error="Telegram didn't accept that. Check the bot token, the chat ID, "
                          "and that the bot is in the group."), 400)


@app.route("/admin/api/logo", methods=["POST"])
@staff_required
def admin_logo_upload():
    f = request.files.get("file")
    if not f or f.mimetype not in ALLOWED_IMAGE:
        return jsonify(error="Upload a PNG, JPG or WebP."), 400
    data = f.read()
    with db_cursor() as cur:
        cur.execute("""INSERT INTO gs_assets (key, mime, data) VALUES ('logo', %s, %s)
                       ON CONFLICT (key) DO UPDATE SET mime = EXCLUDED.mime, data = EXCLUDED.data,
                                                       updated_at = NOW()""",
                    (f.mimetype, psycopg2.Binary(data)))
    return jsonify(ok=True)


@app.route("/admin/api/logo", methods=["DELETE"])
@staff_required
def admin_logo_reset():
    with db_cursor() as cur:
        cur.execute("DELETE FROM gs_assets WHERE key = 'logo'")
    return jsonify(ok=True)


@app.route("/admin/api/users", methods=["POST"])
@staff_required
def admin_user_create():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()[:40]
    pin = str(body.get("pin") or "").strip()
    if not name:
        return jsonify(error="Give them a name."), 400
    if not PIN_RE.match(pin):
        return jsonify(error="A PIN is 4 to 8 digits."), 400
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM gs_users WHERE lower(name) = lower(%s)", (name,))
        if cur.fetchone():
            return jsonify(error="Someone already has that name."), 400
        cur.execute("INSERT INTO gs_users (name, pin_hash) VALUES (%s, %s) RETURNING id",
                    (name, hash_pin(pin)))
        uid = cur.fetchone()["id"]
    return jsonify(ok=True, id=uid), 201


@app.route("/admin/api/users/<int:uid>", methods=["POST"])
@staff_required
def admin_user_update(uid):
    body = request.get_json(silent=True) or {}
    with db_cursor() as cur:
        cur.execute("SELECT * FROM gs_users WHERE id = %s FOR UPDATE", (uid,))
        u = cur.fetchone()
        if u is None:
            return jsonify(error="That person no longer exists."), 404
        sets, vals = [], []
        if body.get("pin"):
            pin = str(body["pin"]).strip()
            if not PIN_RE.match(pin):
                return jsonify(error="A PIN is 4 to 8 digits."), 400
            sets.append("pin_hash = %s"); vals.append(hash_pin(pin))
        if "active" in body:
            on = bool(body["active"])
            if not on:
                cur.execute("SELECT COUNT(*) AS n FROM gs_users WHERE active AND id <> %s", (uid,))
                if cur.fetchone()["n"] == 0:
                    return jsonify(error="That would leave nobody able to sign in."), 400
            sets.append("active = %s"); vals.append(on)
        if not sets:
            return jsonify(error="Nothing to change."), 400
        cur.execute(f"UPDATE gs_users SET {', '.join(sets)} WHERE id = %s", vals + [uid])
    return jsonify(ok=True)


# ---- export ------------------------------------------------------------------

@app.route("/admin/export.csv")
@staff_required
def admin_export():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM gs_orders ORDER BY created_at")
        orders = [serialise_order(r) for r in cur.fetchall()]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["code", "date", "cafe", "phone", "status", "fulfilment", "needed_by",
                "item", "option", "qty", "unit", "price", "line_total",
                "porter_fare", "order_total", "paid", "due", "written_off"])
    for o in orders:
        created = datetime.fromisoformat(o["created_at"]).astimezone(IST).strftime("%Y-%m-%d %H:%M")
        for i, l in enumerate(o["lines"] or [{}]):
            first = i == 0
            w.writerow([o["code"], created, o["cafe_name"], o["phone"], o["status_label"],
                        FULFILMENT.get(o["fulfilment"], ""), o["needed_by"] or "",
                        l.get("name", ""), l.get("variant_label", ""), l.get("qty", ""),
                        l.get("unit", ""), l.get("price", ""), l.get("line_total", ""),
                        o["porter_fare"] if first else "", o["total"] if first else "",
                        o["paid_amount"] if first else "", o["due"] if first else "",
                        "yes" if o["written_off"] and first else ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=goodstuff-orders-{today().isoformat()}.csv"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
