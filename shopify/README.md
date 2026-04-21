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
- `INVENTORY_API_TOKEN` — required for write-mode. Generate a long random
  string (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
  and set it on the backend. While unset, the write endpoints
  (`/api/use`, `/api/restock`, `/api/sync`, `/api/inventory` POST/PUT/DELETE)
  are open to anyone who knows the URL. When set, they require an
  `X-Inventory-Token: <value>` header — the widget sends this automatically
  once an admin clicks "Set admin key" and pastes the token.
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
8. (Write-mode) On the rendered page, click **Set admin key** in the
   widget header and paste the value of `INVENTORY_API_TOKEN` from step 1.
   The key is stored in your browser only (`localStorage`). You'll then
   see per-row **Use** / **Restock** buttons and a **Sync now** action.
   Click **Clear admin key** to drop back to read-only on that device.

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

By default the widget loads in read-only mode (`GET /api/distributors`
and `GET /api/report` only). Admins unlock write mode by clicking
**Set admin key** and pasting the backend's `INVENTORY_API_TOKEN`; all
writes then carry `X-Inventory-Token` and are gated server-side by
`app.py`.

Recommended posture:

- **Always set `INVENTORY_API_TOKEN` in production.** Without it, the
  write endpoints are open to anyone who can reach the backend URL.
- Treat the token like a password. Rotate if staff turnover occurs —
  the widget's "Clear admin key" only clears the local browser; rotating
  the env var revokes it everywhere.
- Optional defence in depth: put the backend behind Cloudflare Access or
  a reverse-proxy with basic auth so the URL itself isn't reachable.

The token is stored in the Shopify domain's `localStorage`, which is
isolated per-browser and per-domain. It is never written to Shopify
itself.
