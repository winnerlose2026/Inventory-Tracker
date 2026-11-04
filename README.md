# H&H Bagels Inventory Tracker

Flask app that tracks bagel inventory across H&H's distributor warehouses
(US Foods, Cheney Brothers, Chefs Warehouse) and the bakery: on-hand counts,
weekly usage, purchase orders, Lineage deliveries, daily production and
order planning. Live at <https://bagel-inventory.onrender.com>.

Current version: see [`VERSION`](VERSION) and [`CHANGELOG.md`](CHANGELOG.md).
`GET /healthz` reports the running version and deployed commit.

## Layout

| Path | What's there |
|---|---|
| `app.py` | Flask app entry point; registers the blueprints |
| `blueprints/` | Route modules (inventory, POs, production, planning, email, delivery, freight, reporting, admin, health, webhooks) |
| `core/` | Shared helpers: auth, caching, HTTP, errors, activity log |
| `integrations/` | Distributor parsers (USF, Cheney, Chefs Warehouse), email scanner, Lineage, Toast, planner and demand model |
| `scripts/` | Cron and one-off ingest scripts; `safe_push.py` pre-push validator |
| `templates/`, `static/` | Web UI |
| `shopify/` | Shopify-embedded widget |
| `tests/` | Pytest suite and fixtures |
| `docs/` | Refactor plan, distributor email drafts, past change notes and handoffs |
| `render.yaml` | Render blueprint (web service + 6h mailbox-scan cron) |

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in secrets
python app.py
```

## Tests

```bash
pip install pytest
python -m pytest -q
```

## Deploying

`main` is production. CI (`.github/workflows/ci.yml`) runs the integrity gate
and the full test suite on every push, then calls the Render deploy hook.
Before pushing, run `python scripts/safe_push.py` — it catches truncated or
NUL-padded files from Dropbox sync.

Releases are tagged `vX.Y.Z`; bump `VERSION` and add a `CHANGELOG.md` entry
with each one.
