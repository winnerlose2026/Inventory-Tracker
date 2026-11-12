# Working on the Inventory Tracker

Flask app for H&H Bagels (`bagel-inventory` on Render): on-hand stock at the
distributor warehouses, purchase orders, Lineage deliveries, daily production.
It is live, people are logged in, and the data is real. Read
`docs/OPERATIONS.md` for how it runs; this file is how to change it.

## The flow, every time

1. **Start from a fresh clone of GitHub**, on a branch:
   `git clone https://github.com/winnerlose2026/Inventory-Tracker && git switch -c <topic>`.
   Never edit, commit or push from the Dropbox copy of the project. Dropbox
   truncates and NUL-pads files mid-sync and has shipped broken builds.
2. Make the change. **One concern per commit.**
3. Before every push, both must pass:
   ```
   python -m pytest -q
   python scripts/safe_push.py
   ```
   Every fix gets at least one test. `safe_push.py` catches truncated and
   NUL-padded files; it needs `pip install pyyaml` to check the YAML files.
4. Push the branch, open a PR, merge to `main` when CI is green.
5. **Deploys are automatic and ordered**: CI runs the suite, calls the web
   service's deploy hook, waits until `/healthz` reports the new commit,
   checks `/readyz`, and only then deploys the 6-hour scan cron. A failed
   check fails the workflow. Do not deploy by hand around it.

## Commit messages

```
Imperative summary, 72 characters or fewer

- what changed
- why (the failure it fixes, in plain words)
```

## Rules that have each cost us data or an outage

- **Never commit anything under `data/`** (it is gitignored: live stock data).
- **Never make a new environment variable mandatory** and never raise at
  import when one is missing: the app must boot with whatever is set.
- **Keep every existing API response field**; add, do not rename or remove.
- **Save on-hand and the ledger together**: use
  `inventory_tracker.save_inventory_and_usage(inv, usage)`, never
  `save_inventory()` then `save_usage()`.
- **A save that shrinks the data is refused** (more than 10% of SKUs, more
  than 2% of ledger rows). Pass `allow_shrink=True` only where removing rows
  is the point.
- **All data files go through `_read_json` / `_write_json`** (atomic, backed
  up, cached). No raw `open(..., "w")` on anything in `data/`.
- **Read-modify-write runs inside `data_lock()`** (`@with_data_lock` on views).
- **A job reports its result**: `core.feed_health.record(name, ok, detail)` on
  success and failure; `raise_issue(key, message)` for something a person
  must look at, `clear_issue(key)` when it is resolved.
- **Adding or removing a route**: regenerate the pinned list with
  `python tests/test_route_manifest.py --write` and commit it.
- Tests never touch the real `data/`: `tests/conftest.py` points every data
  path at a temp folder. Do not call `monkeypatch.undo()` in a test.

## Is it healthy?

`GET /api/feeds/health` (login or `X-Inventory-Token`):

- `ok` is true only when no feed is stale or failing and no issue is open.
- `feeds.<name>`: `last_ok`, `last_attempt`, `detail`, and `stale` (no
  success within `max_age_hours`). A job that stopped shows `stale`; a job
  that runs but fails shows `ok: false` with the reason in `detail`.
- `issues`: specific things for a person (`key`, `message`, `severity`).

The watchdog emails `ALERT_EMAIL_TO` once per new issue and a summary each
morning while anything is open. `/readyz` is the quick yes/no;
`/healthz/deep` has file sizes and details.

## Restoring from a backup

Snapshots of every data file are taken hourly (`data/backups/`, kept 24
hourly / 14 daily / 8 weekly, plus every `pre-*` snapshot for 30 days).

```
GET  /api/admin/backups                         list
POST /api/admin/backups/<name>/restore  {}      dry run: what would change
POST /api/admin/backups/<name>/restore  {"dry_run": false}
```

Always read the dry run first. A live restore snapshots the current state as
`pre-restore` before it writes, so a restore can itself be undone. If the
disk was lost, upload the off-site copy first
(`POST /api/admin/backups/upload`, body = the `.tar.gz`). Full procedure:
`docs/OPERATIONS.md`.
