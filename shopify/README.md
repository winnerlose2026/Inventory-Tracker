# Running the Bagel Inventory Tracker inside Shopify

Shopify cannot host the Python/Flask backend directly. This folder covers
**Option 2**: keep the Flask app running on an external host and render its
data inside your Shopify store through a drop-in widget.

```
 ┌────────────────────────────┐       ┌─────────────────────────────┐
 │  Shopify page / section    │       │  Flask backend (this repo)  │
 │   inventory_widget.liquid  │ ────▶ │   render.com / fly.io / …   │
 │   fetches /api/distributors│       │   /api/distributors         │
 │   renders tables, badges   │       │   /api/report               │
 └────────────────────────────┘       └─────────────────────────────┘
            public Shopify domain          private backend URL
```

## 1. Deploy the Flask backend

Any Python-capable host works. Minimum requirements:

- Python 3.11+
- `pip install -r requirements.txt`
- Persistent writable directory for `data/` (the JSON store)
- Expose port 5000 (or the platform's `$PORT`) over HTTPS

Set environment variables from `.env.example`:

- `ALLOWED_ORIGINS` — comma-separated list of Shopify domains that may call
  the API, e.g.
  ```
  ALLOWED_ORIGINS=https://your-store.myshopify.com,https://your-store.com
  ```
  Leave unset in local dev. Set to `*` only for quick testing.
- `INVENTORY_API_TOKEN` — the backend's master key. **Every `/api/*`
  request needs a login session or a key, reads included**: without one the
  backend answers `401` to `GET /api/distributors` and `GET /api/report`
  just as it does to a write. Generate it with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`, set it on
  the backend, and keep it there: it is for server-side jobs only.
- `INVENTORY_API_TOKENS` — named, limited keys for anything outside the
  backend, e.g. `shopify-view:<long random string>:read`. A `read` key can
  load the tables and nothing else. This is the only kind of key that may
  be typed into the widget.
- Cheney / US Foods creds (if you've been issued them).
- Microsoft 365 creds (`MS365_*`) if you want the email scanner to run.

Start with a platform-provided WSGI server (e.g. `gunicorn app:app`). Note
the public URL — you'll paste it into the widget in step 3.

## 2. Seed the inventory

Run once on the host (or locally against the same `data/` directory):

```
python seed_bagels.py --reset
```

This creates 88 SKUs (11 varieties × 8 warehouses) with case cost, case
size (60 = 5 dozen) and weekly usage populated.

## 3. Add the widget to Shopify

1. Open Shopify admin → **Online Store → Pages → Add page**.
2. Title the page (e.g. "Bagel Inventory"). Under Visibility, choose
   **Visible** or **Hidden** as appropriate.
3. In the rich-text editor toolbar, click the `<>` **Show HTML** button.
4. Paste the full contents of `inventory_widget.liquid`.
5. Near the top of the `<script>` block, replace
   `https://YOUR-BACKEND-HOST.example.com` with the public URL of your
   Flask deployment (no trailing slash).
6. (Optional) Change `SHOW_LOW_ONLY` to `true` to hide healthy SKUs, or
   set `REFRESH_MS` to auto-refresh the page (e.g. `60000` for every
   minute).
7. Save the page. Preview it from the admin.
8. On the rendered page, click **Set admin key** in the widget header and
   paste the **read-only** key from `INVENTORY_API_TOKENS` (step 1). The
   tables load once a key is set; without one every request is refused.
   The key is stored in that browser only (`localStorage`). **Never paste
   the master `INVENTORY_API_TOKEN` into a storefront page**: anything
   running on the storefront domain (theme code, apps, a browser extension)
   can read `localStorage`, and that key can change and delete stock data.
   Use, Restock and Sync are done in the Inventory Tracker itself, signed in.

Alternative: paste the widget into a custom **theme section**
(`sections/bagel-inventory.liquid`) and drop it onto any template in the
theme customizer.

## 4. Verify

Browser dev-tools Network tab, on the Shopify page, should show a
successful `GET /api/distributors` to your backend URL with a
`200 OK` and an `Access-Control-Allow-Origin` header echoing the
Shopify domain. If CORS is misconfigured you'll see the error banner the
widget renders on failure.

## Security notes

- **Nothing is open.** Reads and writes both need a key or a signed-in
  session; the only unauthenticated endpoints are `/healthz`, `/readyz`
  and the login page. If the widget shows data, it is because a key is
  stored in that browser.
- **Only a `read`-scope key belongs in the widget.** It is stored in the
  Shopify domain's `localStorage`, which every script on that domain can
  read. The master `INVENTORY_API_TOKEN` (and any `full` key) must never
  be pasted into a storefront page, a theme file or a Liquid template.
  With a `read` key the widget's Use / Restock / Sync buttons answer
  `403`; make those changes in the app.
- Rotate a widget key by changing its entry in `INVENTORY_API_TOKENS` on
  the backend; "Clear admin key" only clears one browser.
- Set `ALLOWED_ORIGINS` to the exact storefront origins, never `*`.
- Optional defence in depth: put the page behind Shopify's password or a
  staff-only template so the widget is not on a public URL at all.
