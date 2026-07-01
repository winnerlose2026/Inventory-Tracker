"""In-process scheduler for the two lightweight daily jobs that used to be
standalone Render cron services:

  * bagel-inventory-forecast-daily     -> POST /api/forecast/decrement-daily
  * bagel-inventory-graph-sub-renew    -> POST /api/graph/subscriptions/renew

Both cron scripts were thin HTTP clients: they POST an empty body to one of
this web service's own endpoints with the X-Inventory-Token header. Running
them in-process removes two separate Render services with no behavior change.

The heavy 6-hour mailbox scan (scripts/cowork_graph_scan.py) intentionally
stays its own cron: it does outbound Microsoft Graph fetches and pypdf parsing
that we do not want to run inside the web dyno.

Safety
------
* Started exactly once from the gunicorn arbiter via the when_ready hook (see
  gunicorn.conf.py), so the web workers do not each schedule the jobs.
* Both target endpoints are idempotent (forecast is idempotent per SKU + UTC
  date; renew is safe to call repeatedly), so even a duplicate fire — e.g. if
  the service is ever scaled to more than one instance — is harmless.
* Calls stay on localhost (never leaves the container) unless SELF_BASE_URL is
  set explicitly.
* Disable entirely with INPROCESS_SCHEDULER=off.
"""
from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("inproc-scheduler")

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


def _post(path: str, timeout: int = 120) -> None:
    """POST an empty JSON body to one of our own endpoints, authenticated with
    the same INVENTORY_API_TOKEN the crons used. Never raises — a failure is
    logged and the next scheduled run tries again."""
    token = (os.environ.get("INVENTORY_API_TOKEN") or "").strip()
    if not token:
        log.warning("skipping %s - INVENTORY_API_TOKEN not set", path)
        return
    url = f"{_self_base_url()}{path}"
    req = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "X-Inventory-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        log.info("%s -> HTTP %s %s", path, resp.status, body[:500])
    except urllib.error.HTTPError as exc:
        log.error(
            "%s -> HTTP %s: %s",
            path,
            exc.code,
            exc.read().decode("utf-8", errors="replace")[:500],
        )
    except Exception as exc:  # noqa: BLE001 - log and let the next run retry
        log.error("%s failed: %s: %s", path, type(exc).__name__, exc)


def _run_forecast_decrement() -> None:
    _post("/api/forecast/decrement-daily")


def _run_graph_renew() -> None:
    _post("/api/graph/subscriptions/renew")


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
    sched.start()
    _scheduler = sched
    log.info(
        "in-process scheduler started: forecast-decrement 05:05 UTC, "
        "graph-renew 09:00 UTC (base=%s)",
        _self_base_url(),
    )
    return sched
