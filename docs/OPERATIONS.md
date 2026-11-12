# Operations

## Architecture

One Render web service, `bagel-inventory` (Python 3.14, gunicorn: 2 workers x
4 threads, region virginia), serves the Flask app and stores **all state as
JSON files on one 1 GB disk mounted at `data/`**. There is no database. Writes
are atomic (temp file + rename, previous copy kept as `<name>.bak`),
serialised across workers by a file lock (`data/.data.lock`), and on-hand +
ledger are committed together through a small journal (`data/.txn`). An
in-process scheduler (APScheduler, started once by the gunicorn arbiter) runs
the light jobs by POSTing to the app's own endpoints on localhost. One Render
cron runs the heavy mailbox scan every 6 hours; cloud scheduled tasks run the
chasers and the off-site backup pull.

## Jobs

| Job (feed name) | When (UTC) | Where | What |
|---|---|---|---|
| mailbox-scan | 00/06/12/18:00 | Render cron `bagel-inventory-6h-scan` | `POST /api/email/scan`, then `/api/freight/scan` |
| forecast-burn | 05:05 | in-process | daily usage off stock; fills missed days (last 7) |
| toast-nightly | 08:15 | in-process | `POST /api/toast/nightly-pull` |
| graph-renew | 09:00, 21:00 | in-process | renew mailbox subscriptions (expire after ~71 h) |
| production-scan | 23:30 | in-process | Daily Production sheets, 14-day lookback |
| delivery-sync | :07/:27/:47, 10:00-03:59 | in-process | read Lineage delivery confirmations |
| backup | hourly :20 | in-process | snapshot `data/*.json` to `data/backups/` |
| watchdog | hourly :40 | in-process | checks below; alert email; books check 07:40; digest 13:40 |
| scheduler | every 5 min | in-process | heartbeat |
| catch-up | 90 s after start | in-process | re-runs any job that missed its slot |
| lineage-po-followups | 14:50, 20:50 Mon-Fri | Render cron (to be created) | `scripts/lineage_followups.py` |
| backup-offsite | daily | cloud task | pulls `/api/admin/backups/latest`, then `POST /api/admin/backup/offsite-ack` |
| inventory chasers | Mon/Tue | cloud tasks | remind warehouses for the weekly count (`/api/email/send`) |

`INPROCESS_SCHEDULER=off` stops every in-process job.

## Environment variables (web service unless noted)

| Variable | Default | Meaning |
|---|---|---|
| `INVENTORY_API_TOKEN` | none | Master key, full access. The in-process jobs need it. Also on the scan cron. |
| `INVENTORY_API_TOKENS` | none | Extra keys: `name:token:scope,...`; scope `full`, `read`, `ingest`, `send`, `backup` (join with `+`). |
| `DELIVERY_API_TOKEN` | none | Limited key for the Lineage delivery task and the follow-ups cron. |
| `INVENTORY_USERNAMES` | none | Login allowlist, comma-separated. |
| `INVENTORY_PASSWORD` | none | The one shared login password. |
| `INVENTORY_ADMINS` | empty = everyone | Who may run seed reset / restore / backup restore. |
| `FLASK_SECRET_KEY` | derived from the API token | Signs the login cookie. Changing it signs everyone out. |
| `SESSION_REQUIRE_PWV` | `off` | `on` = logins from before password-versioned sessions stop working. |
| `ALERT_EMAIL_TO` | `jd@hhbagels.com` | Where alert emails go (comma-separated). |
| `ALERTS_ENABLED` | `on` | `off` = evaluate and record, send nothing. |
| `APP_URL` | the Render URL | Link in alert emails; base URL for the crons. |
| `FORECAST_BACKFILL_DAYS` | `7` | Missed burn days the 05:05 job may fill; `0` = none. |
| `MAX_UPLOAD_MB` | `25` | Largest request body. |
| `INPROCESS_SCHEDULER` | `on` | See above. |
| `MS365_TENANT_ID`, `MS365_CLIENT_ID`, `MS365_CLIENT_SECRET` | none | Microsoft Graph app (read mail, send mail). |
| `MS365_USER`, `MS365_FOLDER` | none, `Inbox` | Mailboxes scanned (comma-separated). |
| `MS365_SEND_AS` | first `MS365_USER` | Sender for `/api/email/send` and alerts. |
| `EMAIL_SEND_ALLOWED_DOMAINS`, `EMAIL_SEND_ALLOWED_FROM` | built in | Who may be emailed / named as sender. |
| `GRAPH_WEBHOOK_BASE`, `GRAPH_SUB_CLIENT_STATE` | none | Real-time Daily Production webhook. |
| `TOAST_CLIENT_ID`, `TOAST_CLIENT_SECRET`, `TOAST_API_HOSTNAME`, `TOAST_USER_ACCESS_TYPE` | none | Toast sales pulls. |
| `PO_LEAD_DAYS` | `30` | Placeholder ETA for a PO with no ship date. |
| `ALLOWED_ORIGINS` | none | CORS allowlist (Shopify embed). |
| `WEB_CONCURRENCY`, `GUNICORN_THREADS`, `GUNICORN_TIMEOUT` | 2, 4, 180 | gunicorn. |
| `MAILBOXES`, `SCAN_MODE`, `USF_SCREENSHOT_OCR` | (scan cron) | See `render.yaml`. |

GitHub Actions secrets: `RENDER_DEPLOY_HOOK_URL` (web), optional
`RENDER_CRON_DEPLOY_HOOK_URL` (scan cron, deployed only after the web
service is healthy).

## Health endpoints

- `/healthz` open, shallow: version + commit. Render's health check.
- `/readyz` open: `ok` + reason codes (`data_dir_not_writable`,
  `inventory_unreadable`, `inventory_empty`, `scheduler_no_heartbeat`,
  `scheduler_heartbeat_stale`, `data_lock_busy`, `disk_full`).
- `/healthz/deep` (login/token): the same with details and file sizes.
- `/api/feeds/health` (login/token): every feed and every open issue.

## What an alert means

| Issue key | Meaning | First thing to do |
|---|---|---|
| `feed-stale:<job>` | The job has not succeeded within its allowed time. | Check Render logs for that job; for `mailbox-scan`, the cron service. |
| `feed-failing:<job>` | It ran but failed for 3+ hours. | Read `detail` in `/api/feeds/health`. |
| `scheduler-no-token` | `INVENTORY_API_TOKEN` is unset; no in-process job can run. | Set it in the dashboard. |
| `unparsed:<id>` | A report email was recognised but could not be read (6+ h). | Open the email; enter the count by hand or fix the parser. |
| `scan-po-failure:<id>` | A PO email was dropped by the last scan. | Open the PO; it is NOT on the Pending tab. |
| `warehouse-stale:<name>` | No count from that warehouse in 8+ days. | Chase the rep. |
| `books:<sku>` | On-hand does not equal last count + deliveries - usage since. | Compare the ledger for that SKU; correct on-hand. |
| `burn-missing` | Yesterday's usage was not taken off stock. | Check the `forecast-burn` feed. |
| `backup-failed`, `backup-disk-low` | The hourly snapshot did not complete / under 150 MB free. | Free space; `POST /api/admin/backup/run`. |
| `disk-low` | Data disk over 80% full. | Download and prune `data/backups/`, or grow the disk. |
| `data-corrupt:<file>` | A file was unreadable and was restored from its `.bak`. | Check the latest changes are present; the bad copy is `<file>.corrupt-<ts>`. |
| `data-commit-rolled-back` | A save was interrupted and abandoned. | Redo the last change made before the restart. |
| `save-refused:inventory` / `:usage` | A save that would have dropped many rows was blocked. | Nothing was lost; find what ran (activity log). |
| `inventory-empty`, `inventory-unreadable` | 0 SKUs / the file will not parse. | Restore (below). |
| `audit-write-failed` | A change was saved but its history row was not. | Usually disk space. |
| `data-nonfinite:<file>` | A NaN/Infinity number is stored in the file. | Find and correct the value. |

Issues clear themselves when the condition goes away.

## Restore procedure

1. Stop making changes. `GET /api/admin/backups` and pick the snapshot from
   before the problem (names are `<UTC time>-<label>.tar.gz`).
2. Dry run: `POST /api/admin/backups/<name>/restore` with `{}`. Read the
   per-file summary: SKUs added / dropped / changed, ledger `rows_delta`.
3. Apply: same call with `{"dry_run": false}`. To restore only some files
   add `"files": ["inventory.json", "usage.json"]`. The current state is
   saved first as `...-pre-restore.tar.gz`; restore that to undo.
4. `feed_health.json` and `graph_subscriptions.json` are skipped unless named.
5. Anything ingested after the snapshot is gone: run the mailbox scan
   (`POST /api/email/scan`, 14-day lookback) to re-read recent POs and counts.
6. Disk lost entirely: redeploy, then `POST /api/admin/backups/upload` with
   the off-site `.tar.gz` as the body, and restore it as above.

A single file can also be recovered by hand from `data/<name>.bak` (the
previous write) over a Render shell.

`POST /api/admin/restore-inventory` (rebuild `inventory.json` from a saved
`/api/inventory` response) still exists: `dry_run` defaults to true and
`"confirm": true` is required when SKUs would be dropped.
