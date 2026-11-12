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
| `blueprints/` | Route modules (inventory, POs, production, planning, email, delivery, freight, reporting, admin, ops (backups, feed health, watchdog), health, webhooks) |
| `core/` | Shared helpers: auth, caching, HTTP, errors, activity log |
| `integrations/` | Distributor parsers (USF, Cheney, Chefs Warehouse), email scanner, Lineage, Toast, planner and demand model |
| `scripts/` | Cron and one-off ingest scripts; `safe_push.py` pre-push validator |
| `templates/`, `static/` | Web UI |
| `shopify/` | Shopify-embedded widget |
| `tests/` | Pytest suite and fixtures |
| `docs/` | `OPERATIONS.md` (jobs, env vars, alerts, restore), refactor plan, distributor email drafts, past change notes and handoffs |
| `CLAUDE.md` | How to make a change: the dev flow and the rules |
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

`main` is production. Work in a fresh clone on a branch (never the Dropbox
copy) and run `python -m pytest -q` and `python scripts/safe_push.py` before
every push -- see [`CLAUDE.md`](CLAUDE.md). CI
(`.github/workflows/ci.yml`) runs the integrity gate and the full suite on
every push, then calls the Render deploy hook, waits for `/healthz` to report
the new commit, checks `/readyz`, and only then deploys the scan cron.

Is it healthy? `GET /api/feeds/health`. How it runs, what each alert means
and how to restore from a backup: [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

Releases are tagged `vX.Y.Z`; bump `VERSION` and add a `CHANGELOG.md` entry
with each one.
