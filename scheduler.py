"""In-process scheduler: the jobs that run INSIDE the web service.

Each job POSTs to one of this service's own endpoints on localhost with the
X-Inventory-Token header (they began as thin Render cron scripts that did the
same from outside). All times UTC.

  job                     when                      endpoint
  forecast-burn           05:05 daily               POST /api/forecast/decrement-daily
                                                    (today, plus any of the last 7
                                                    days that has no burn at all)
  graph-renew             09:00 and 21:00           POST /api/graph/subscriptions/renew
                          + hourly :35 while a renew failure is open
  production-scan         23:30 daily               POST /api/production/scan
  delivery-sync           :07/:27/:47, 10:00-03:59  POST /api/pos/delivery/sync-threads
  toast-nightly           08:15 daily               POST /api/toast/nightly-pull
  backup                  hourly at :20             POST /api/admin/backup/run
  watchdog                hourly at :40             POST /api/watchdog/run
  scheduler (heartbeat)   every 5 minutes           (writes feed health directly)
  catch-up                once, ~90 s after start   re-runs a job whose last
                                                    success is older than its period

Every run is recorded in core.feed_health under the job's name (ok / detail),
so "it stopped" and "it runs but fails" both reach /api/feeds/health and the
watchdog instead of only stderr.

The heavy 6-hour mailbox scan (scripts/cowork_graph_scan.py) intentionally
stays its own cron: it does outbound Microsoft Graph fetches and pypdf parsing
that we do not want to run inside the web dyno.

Safety
------
* Started exactly once from the gunicorn arbiter via the when_ready hook (see
  gunicorn.conf.py), so the web workers do not each schedule the jobs.
* Every target endpoint is idempotent (forecast is idempotent per SKU + UTC
  date; renew is safe to call repeatedly), so even a duplicate fire -- e.g. if
  the service is ever scaled to more than one instance -- is harmless.
* Calls stay on localhost (never leaves the container) unless SELF_BASE_URL is
  set explicitly.
* Disable entirely with INPROCESS_SCHEDULER=off.
"""
from __future__ import annotations

import logging
import os
import sys
import urllib.error
import urllib.request

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

# gunicorn configures its own loggers but not arbitrary ones, so a bare logger
# defaults to WARNING and drops our INFO lines (the "started" confirmation and
# each run's POST result). Attach a stderr handler at INFO once and don't
# propagate, so the scheduler's activity is visible in Render logs without
# double-printing under the root/gunicorn loggers.
log = logging.getLogger("inproc-scheduler")
if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(
        logging.Formatter("[%(asctime)s] [inproc-scheduler] [%(levelname)s] %(message)s")
    )
    log.addHandler(_handler)
log.setLevel(logging.INFO)
log.propagate = False

# APScheduler 3.x requires a pytz timezone (its 3.x internals reject a plain
# zoneinfo tzinfo with "Only timezones from the pytz library are supported").
UTC = pytz.utc

_scheduler: BackgroundScheduler | None = None


def _self_base_url() -> str:
    """Base URL of this very service. Prefer localhost so the scheduled call
    never leaves the container; allow an explicit override for unusual setups."""
    explicit = (os.environ.get("SELF_BASE_URL") or "").rstrip("/")
    if explicit:
        return explicit
    return f"http://127.0.0.1:{os.environ.get('PORT', '10000')}"


def _record(feed: str | None, ok: bool, detail: str) -> None:
    """Note a job run in the shared feed-health record. Never raises."""
    if not feed:
        return
    try:
        from core import feed_health
        feed_health.record(feed, ok, detail)
    except Exception as exc:  # noqa: BLE001
        log.error("could not record feed %s: %s", feed, type(exc).__name__)


def _issue(key: str, message: str | None, severity: str = "error") -> None:
    """Open (message given) or clear (None) an issue. Never raises."""
    try:
        from core import feed_health
        if message is None:
            feed_health.clear_issue(key)
        else:
            feed_health.raise_issue(key, message, severity)
    except Exception as exc:  # noqa: BLE001
        log.error("could not update issue %s: %s", key, type(exc).__name__)


def _request(method: str, path: str, timeout: int = 120,
             payload: dict | None = None, feed: str | None = None,
             optional: bool = False):
    """Call one of our own endpoints with the INVENTORY_API_TOKEN. Returns
    (status, parsed JSON or None); status is 0 when the call never happened.
    Records the outcome under `feed`. Never raises -- a failure is logged and
    recorded, and the next scheduled run tries again.

    optional=True: a 404/405 means the endpoint is not deployed yet; that is
    logged at INFO and recorded nowhere."""
    import json as _json
    token = (os.environ.get("INVENTORY_API_TOKEN") or "").strip()
    if not token:
        # Used to be a silent skip: every job quietly did nothing.
        log.error("cannot run %s - INVENTORY_API_TOKEN is not set", path)
        _record(feed, False, "INVENTORY_API_TOKEN is not set")
        _issue("scheduler-no-token",
               "Scheduled jobs cannot run: INVENTORY_API_TOKEN is not set on "
               "the web service.")
        return 0, None
    url = f"{_self_base_url()}{path}"
    data = None
    headers = {"X-Inventory-Token": token}
    if method != "GET":
        data = _json.dumps(payload or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    status, text = 0, ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            text = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text = ""
    except Exception as exc:  # noqa: BLE001 - log and let the next run retry
        log.error("%s failed: %s: %s", path, type(exc).__name__, exc)
        _record(feed, False, f"{type(exc).__name__}: {exc}"[:300])
        return 0, None
    parsed = None
    try:
        parsed = _json.loads(text) if text else None
    except ValueError:
        parsed = None
    if optional and status in (404, 405):
        log.info("%s -> HTTP %s (not deployed yet)", path, status)
        return status, parsed
    ok = 200 <= status < 300 and not (
        isinstance(parsed, dict) and parsed.get("ok") is False)
    if ok:
        log.info("%s -> HTTP %s %s", path, status, text[:500])
    else:
        log.error("%s -> HTTP %s: %s", path, status, text[:500])
    detail = f"HTTP {status}"
    if isinstance(parsed, dict) and parsed.get("error"):
        detail += f": {str(parsed.get('error'))[:200]}"
    _record(feed, ok, detail)
    return status, parsed


def _post(path: str, timeout: int = 120, payload: dict | None = None,
          feed: str | None = None, optional: bool = False):
    """POST a JSON body to one of our own endpoints. See _request."""
    return _request("POST", path, timeout, payload, feed, optional)


def _backfill_days() -> int:
    try:
        return max(0, min(14, int(os.environ.get("FORECAST_BACKFILL_DAYS", "7"))))
    except (TypeError, ValueError):
        return 7


def _run_forecast_decrement() -> None:
    """Take today's usage off stock, and fill any of the last 7 UTC days the
    burn never ran for (service down at 05:05, a failed run).

    A past day is filled only when it has NO burn rows at all (asked with a
    dry run first), and only for warehouses whose SKUs have not been counted
    on or after that day: a count is the truth as of its own date, so burning
    a day the count already covers would take the cases off twice.
    """
    from datetime import datetime, timedelta, timezone
    path = "/api/forecast/decrement-daily"
    if not (os.environ.get("INVENTORY_API_TOKEN") or "").strip():
        _post(path, feed="forecast-burn")      # records the failure + issue
        return
    today = datetime.now(timezone.utc).date()
    filled, notes = [], []
    days = _backfill_days()
    inv = None
    for back in range(days, 0, -1):
        day = (today - timedelta(days=back)).isoformat()
        status, res = _post(path, payload={"date": day, "dry_run": True})
        if not (200 <= status < 300) or not isinstance(res, dict):
            notes.append(f"{day}: could not check")
            continue
        if res.get("skipped_existing") or not res.get("applied"):
            continue                       # burned already, or nothing to burn
        if inv is None:
            _st, inv = _request("GET", "/api/inventory")
            if not isinstance(inv, list):
                notes.append("backfill skipped: inventory unreadable")
                break
        by_wh: dict = {}
        for item in inv:
            if float(item.get("weekly_usage") or 0) <= 0:
                continue
            wh = item.get("warehouse") or ""
            counted_since = (item.get("last_count_at") or "")[:10] >= day
            by_wh[wh] = by_wh.get(wh, False) or counted_since
        done = 0
        for wh, counted_since in sorted(by_wh.items()):
            if not wh or counted_since:
                continue
            st2, r2 = _post(path, payload={"date": day, "warehouse_prefix": wh})
            if 200 <= st2 < 300 and isinstance(r2, dict):
                done += len(r2.get("applied") or [])
        if done:
            filled.append(f"{day} ({done} SKUs)")
    status, res = _post(path)
    ok = 200 <= status < 300 and isinstance(res, dict) and res.get("ok") is not False
    detail = (f"today: {len(res.get('applied') or [])} burned, "
              f"{len(res.get('skipped_existing') or [])} already done"
              if ok else f"HTTP {status}")
    if filled:
        detail += "; filled missed day(s): " + ", ".join(filled)
    if notes:
        detail += "; " + "; ".join(notes)
    if status:                              # 0 = no token; already recorded
        _record("forecast-burn", ok, detail[:480])


def _run_graph_renew() -> None:
    _post("/api/graph/subscriptions/renew", feed="graph-renew")


def _run_graph_renew_retry() -> None:
    """Hourly at :35, but only while a renew failure is open: a Microsoft
    503 at 09:00 used to wait twelve hours for the 21:00 run (2026-11-13)."""
    try:
        from integrations.graph_subscriptions import renew_failure_pending
        if not renew_failure_pending():
            return
    except Exception:  # noqa: BLE001
        return
    _post("/api/graph/subscriptions/renew", feed="graph-renew")


def _run_production_scan() -> None:
    """Ingest Daily Production sheets from the mailbox.

    Nothing scheduled this before, so /api/production/scan only ran when
    someone remembered to call it by hand -- the last time was 2026-06-22 and
    the tab silently sat ten weeks behind.

    The 14-day lookback is deliberate overlap, not thoroughness for its own
    sake: ingestion is idempotent on source_message_id, so a run that fails
    or a day the service is down heals itself on the next run instead of
    leaving a permanent hole. The timeout is generous because each sheet
    costs a Graph attachment fetch plus a pypdf parse.
    """
    _post("/api/production/scan", timeout=600,
          payload={"lookback_days": 14, "max_messages": 200},
          feed="production-scan")


def _run_delivery_sync() -> None:
    """Re-read the Lineage "Quote and Schedule" threads so a delivery
    confirmation reaches the Pending POs tab (and on-hand) within ~20 minutes
    of landing, instead of at the cloud task's next 9:22 / 3:22 run. The sync
    only reads mail and applies plain confirmations -- it never sends a
    follow-up (that stays with the task's send-followups call). 60-day
    lookback so no open thread drops out of the cache between task runs."""
    _post("/api/pos/delivery/sync-threads", timeout=170,
          payload={"lookback_days": 60}, feed="delivery-sync")


def _run_toast_nightly() -> None:
    """Pull yesterday's Toast sales. The endpoint ships separately: until it
    exists the 404 is logged at INFO and nothing is recorded."""
    _post("/api/toast/nightly-pull", timeout=600, feed="toast-nightly",
          optional=True)


def _run_backup() -> None:
    """Hourly snapshot of every data file (integrations/backups.py)."""
    # The endpoint records feed "backup" itself (with the snapshot's name);
    # only a call that never produced an answer is recorded from here.
    status, res = _post("/api/admin/backup/run", timeout=300,
                        payload={"label": "hourly"})
    if status and not isinstance(res, dict):
        _record("backup", False, f"HTTP {status}")


def _run_watchdog() -> None:
    """Hourly health check + alert email (integrations/watchdog.py). The
    watchdog records its own feed; this only notes a run that never got
    there."""
    status, _res = _post("/api/watchdog/run", timeout=300)
    if status and not (200 <= status < 300):
        _record("watchdog", False, f"HTTP {status}")


def _heartbeat() -> None:
    """Every 5 minutes: prove the scheduler thread is alive (/readyz and the
    watchdog read feed "scheduler")."""
    n = len(_scheduler.get_jobs()) if _scheduler is not None else 0
    _record("scheduler", True, f"{n} job(s) scheduled")
    if (os.environ.get("INVENTORY_API_TOKEN") or "").strip():
        _issue("scheduler-no-token", None)


# feed -> (runner, hours after which a missed run is made up at start-up,
#          run at start-up even if it has never run before?)
_CATCH_UP = (
    ("backup", _run_backup, 1.25, True),
    ("forecast-burn", _run_forecast_decrement, 25, True),
    ("graph-renew", _run_graph_renew, 25, False),
    ("production-scan", _run_production_scan, 25, False),
    ("toast-nightly", _run_toast_nightly, 25, False),
)


def _catch_up(now=None) -> list:
    """Run once shortly after start: APScheduler keeps its jobs in memory, so
    a run that fell while the service was down or deploying was simply lost.
    Re-runs each job whose last success is older than its period. Returns the
    feeds it ran."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    try:
        from core import feed_health
        feeds = feed_health.snapshot().get("feeds") or {}
    except Exception as exc:  # noqa: BLE001
        log.error("catch-up: feed health unreadable: %s", type(exc).__name__)
        return []
    ran = []
    for feed, runner, hours, even_if_never in _CATCH_UP:
        last_ok = (feeds.get(feed) or {}).get("last_ok") or ""
        due = False
        if not last_ok:
            due = even_if_never
        else:
            try:
                dt = datetime.fromisoformat(last_ok.replace("Z", "+00:00"))
                due = (now - dt) > timedelta(hours=hours)
            except ValueError:
                due = even_if_never
        if not due:
            continue
        log.info("catch-up: running %s (last ok: %s)", feed, last_ok or "never")
        try:
            runner()
        except Exception as exc:  # noqa: BLE001
            log.error("catch-up %s failed: %s: %s", feed,
                      type(exc).__name__, exc)
        ran.append(feed)
    return ran


def start_scheduler() -> BackgroundScheduler | None:
    """Start the background scheduler once. Idempotent within a process; a
    no-op when INPROCESS_SCHEDULER is turned off."""
    global _scheduler
    if (os.environ.get("INPROCESS_SCHEDULER", "on").strip().lower()
            in {"off", "0", "false", "no"}):
        log.info("in-process scheduler disabled via INPROCESS_SCHEDULER")
        return None
    if _scheduler is not None:
        return _scheduler

    sched = BackgroundScheduler(timezone=UTC)
    # Forecast decrement — was Render cron "5 5 * * *" (daily 05:05 UTC).
    sched.add_job(
        _run_forecast_decrement,
        CronTrigger(hour=5, minute=5, timezone=UTC),
        id="forecast-decrement-daily",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # Graph subscription renew — was Render cron "0 9 * * *" (daily 09:00 UTC).
    sched.add_job(
        _run_graph_renew,
        CronTrigger(hour=9, minute=0, timezone=UTC),
        id="graph-subscriptions-renew",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # Daily Production sheet ingest — 23:30 UTC (7:30pm ET), after the
    # production day's sheets have been sent.
    sched.add_job(
        _run_production_scan,
        CronTrigger(hour=23, minute=30, timezone=UTC),
        id="production-scan-daily",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # Second renew attempt 12 hours later: mail subscriptions expire after
    # ~71 h, so one failed 09:00 run used to leave a single chance to save
    # them.
    sched.add_job(
        _run_graph_renew,
        CronTrigger(hour=21, minute=0, timezone=UTC),
        id="graph-subscriptions-renew-pm",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # Renew retry -- hourly at :35, a no-op unless the last renew failed.
    sched.add_job(
        _run_graph_renew_retry,
        CronTrigger(minute=35, timezone=UTC),
        id="graph-subscriptions-renew-retry",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    # Lineage delivery confirmations — every 20 min, 6am-11pm ET (10-03 UTC).
    sched.add_job(
        _run_delivery_sync,
        CronTrigger(minute="7,27,47", hour="0-3,10-23", timezone=UTC),
        id="delivery-sync-20m",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    # Nightly Toast sales pull — 08:15 UTC.
    sched.add_job(
        _run_toast_nightly,
        CronTrigger(hour=8, minute=15, timezone=UTC),
        id="toast-nightly-pull",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # Hourly backup at :20 and watchdog at :40.
    sched.add_job(
        _run_backup,
        CronTrigger(minute=20, timezone=UTC),
        id="backup-hourly",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    sched.add_job(
        _run_watchdog,
        CronTrigger(minute=40, timezone=UTC),
        id="watchdog-hourly",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    # Heartbeat every 5 minutes.
    sched.add_job(
        _heartbeat,
        IntervalTrigger(minutes=5, timezone=UTC),
        id="scheduler-heartbeat",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    # One catch-up pass ~90 s after start, once the workers are serving.
    from datetime import datetime, timedelta
    sched.add_job(
        _catch_up,
        DateTrigger(run_date=datetime.now(UTC) + timedelta(seconds=90),
                    timezone=UTC),
        id="catch-up-once",
        max_instances=1,
        misfire_grace_time=600,
    )
    sched.start()
    _scheduler = sched
    try:
        _heartbeat()
    except Exception:  # noqa: BLE001 -- never let a health write stop start-up
        pass
    log.info(
        "in-process scheduler started: forecast-burn 05:05 UTC, "
        "graph-renew 09:00 + 21:00 UTC, production-scan 23:30 UTC, "
        "delivery-sync every 20 min 10:00-03:59 UTC, toast-nightly 08:15 UTC, "
        "backup hourly :20, watchdog hourly :40, heartbeat every 5 min, "
        "catch-up in 90 s (base=%s)",
        _self_base_url(),
    )
    return sched
