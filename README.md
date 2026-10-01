# The Good Stuff — trade orders for cafes

This is a single Flask app for selling coffee beans to cafes. It has no public shop, no kitchen and no riders.

| Path | Who | What |
| --- | --- | --- |
| `/` | — | Redirects to `/cafe` |
| `/cafe` | cafes | Sign in with phone + PIN, order at their own prices, see orders and what they owe |
| `/o/<code>-<token>` | anyone with the link | One order's bill, with a UPI QR for what's still due |
| `/s/<id>-<token>` | anyone with the link | A cafe's statement: every unpaid order, one QR for the total |
| `/admin` | staff | Orders, Money owed, Cafes, Products, Settings |
| `/signin` | staff | Your own name and PIN. Everyone is a super admin |

## How an order moves

1. **The cafe orders** on `/cafe`, or you type it in under **Orders → New order** for a phone or WhatsApp order. Both go through the same code, so prices and bills come out identical.
2. **Received.** If Telegram is set up, the group gets an alert.
3. **You book Porter** and press **Add fare** on the order. The fare is added to the cafe's bill. If the cafe collects the order itself, choose *Cafe collects* and there is no fare.
4. **Ready for pickup** → **Picked up by Porter** (or **Collected**). After each step a bar offers to send the cafe the update on WhatsApp.
5. **Money owed** groups unpaid orders by cafe. **Send bill on WhatsApp** writes the statement for you and links to a page with the UPI QR. **Record payment** takes a lump sum and applies it to their oldest bills first. **Undo** reverses a payment exactly.

Items can be edited while an order is still *Received*. After that they're locked. Prices are always recalculated on the server from the cafe's price sheet, never taken from the browser.

## Products

A product is something on the list (*Ethiopia Yirgacheffe*). Its **options** are what a cafe actually picks, and each option has its own list price per unit:

- Raw — ₹900 / kg
- Roasted – Morning Light — ₹1,400 / kg
- Roasted – Dark Knight — ₹1,450 / kg

**Sold per** is `kg` for beans. Later items work the same way with a different unit and an optional minimum order: cups (`box of 1000`), syrups (`case of 6`), napkins, dairy, desserts. **Category** groups them on the cafe's screen. Untick **Show to cafes** to pause a product without deleting it.

## Cafe prices

Every cafe pays list price unless you change it. Under **Cafes → Prices** you can, per option:

- type a price to give that cafe its own rate, or
- untick **Offer** to hide that option from them.

**Copy prices from…** starts a new cafe's sheet from an existing one. When you add a cafe, the app offers to WhatsApp them their login link and PIN.

## Deploy on Render

| Setting | Value |
| --- | --- |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60` |

| Env var | |
| --- | --- |
| `DATABASE_URL` | Postgres connection string. Can be the same database as the ERP or Sugarcrush |
| `DB_SCHEMA` | Optional, defaults to `goodstuff`. The app keeps all its tables in this schema and never touches anyone else's |
| `ADMIN_PIN` | Creates the first account (named `ADMIN_NAME`, default **Moiz**) and acts as an emergency login. **Remove it** once your own PIN works |
| `ADMIN_NAME` | Optional, defaults to `Moiz` |
| `SECRET_KEY` | A long random string. Without it, everyone is signed out on each deploy and bill links already sent stop working |

Tables are created on first boot. To remove the app completely, run `DROP SCHEMA goodstuff CASCADE`.

### After the first deploy

1. Sign in at `/signin` as Moiz with `ADMIN_PIN`.
2. **Settings:** UPI ID, WhatsApp number and pickup address.
3. **Products:** add your beans.
4. **Cafes:** add each cafe and send them the login.
5. Under **Settings → People**, give yourself a PIN of your own, then delete `ADMIN_PIN` from Render.

The Good Stuff logo is built in. A different logo can be uploaded in **Settings**.

## Tests

```bash
PGHOST=/tmp PGPORT=5433 PGUSER=postgres ./tests/run_tests.sh
```

This creates a fresh database on every run. The tests cover pricing, the price sheet, minimum orders, Porter fare, payments (oldest first, overpaying, undo), cancelling, private links and the people rules.
