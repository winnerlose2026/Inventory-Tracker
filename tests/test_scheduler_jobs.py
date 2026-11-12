"""In-process scheduler: feed recording, catch-up, burn backfill, job list."""
import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("apscheduler")

import scheduler  # noqa: E402
from core import feed_health  # noqa: E402


class _Resp:
    def __init__(self, status, body):
        self.status, self._b = status, json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def http(monkeypatch):
    """Fake urlopen; `calls` collects (method, path, payload)."""
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    calls, routes = [], {}

    def urlopen(req, timeout=0):
        path = req.full_url.split(":10000", 1)[-1]
        payload = json.loads(req.data.decode()) if req.data else None
        assert req.get_header("X-inventory-token") == "tok"
        calls.append((req.get_method(), path, payload))
        status, body = routes.get(path, lambda p: (200, {"ok": True}))(payload)
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {},
                                         io.BytesIO(json.dumps(body).encode()))
        return _Resp(status, body)
    monkeypatch.setattr(scheduler.urllib.request, "urlopen", urlopen)
    monkeypatch.delenv("SELF_BASE_URL", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    return calls, routes


def _feeds():
    return feed_health.snapshot()["feeds"]


def test_missing_token_is_a_recorded_failure_not_a_silent_skip(monkeypatch):
    monkeypatch.delenv("INVENTORY_API_TOKEN", raising=False)
    scheduler._run_graph_renew()
    snap = feed_health.snapshot()
    assert snap["feeds"]["graph-renew"]["ok"] is False
    assert "INVENTORY_API_TOKEN" in snap["feeds"]["graph-renew"]["detail"]
    assert "scheduler-no-token" in snap["issues"]
    scheduler._run_forecast_decrement()
    assert feed_health.snapshot()["feeds"]["forecast-burn"]["ok"] is False


def test_post_records_success_and_failure(http):
    calls, routes = http
    scheduler._run_graph_renew()
    assert _feeds()["graph-renew"]["ok"] is True
    routes["/api/graph/subscriptions/renew"] = lambda p: (500, {"error": "boom"})
    scheduler._run_graph_renew()
    f = _feeds()["graph-renew"]
    assert f["ok"] is False and "500" in f["detail"] and f["last_ok"]
    # HTTP 200 with {"ok": false} is a failure too
    routes["/api/production/scan"] = lambda p: (200, {"ok": False, "error": "graph"})
    scheduler._run_production_scan()
    assert _feeds()["production-scan"]["ok"] is False


def test_connection_error_is_recorded(monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")

    def boom(req, timeout=0):
        raise ConnectionRefusedError("nope")
    monkeypatch.setattr(scheduler.urllib.request, "urlopen", boom)
    scheduler._run_delivery_sync()
    assert _feeds()["delivery-sync"]["ok"] is False


def test_endpoint_not_deployed_yet_is_not_an_error(http):
    calls, routes = http
    routes["/api/toast/nightly-pull"] = lambda p: (404, {"error": "nf"})
    scheduler._run_toast_nightly()
    assert calls[-1][:2] == ("POST", "/api/toast/nightly-pull")
    assert calls[-1][2] == {}
    assert "toast-nightly" not in _feeds()
    routes["/api/toast/nightly-pull"] = lambda p: (200, {"ok": True})
    scheduler._run_toast_nightly()
    assert _feeds()["toast-nightly"]["ok"] is True


def test_backup_and_watchdog_jobs_call_their_endpoints(http):
    calls, routes = http
    scheduler._run_backup()
    scheduler._run_watchdog()
    assert ("POST", "/api/admin/backup/run", {"label": "hourly"}) in calls
    assert ("POST", "/api/watchdog/run", {}) in calls
    routes["/api/watchdog/run"] = lambda p: (500, {})
    scheduler._run_watchdog()
    assert _feeds()["watchdog"]["ok"] is False


def test_forecast_fills_only_empty_days_and_skips_counted_warehouses(http):
    calls, routes = http
    today = datetime.now(timezone.utc).date()
    missed = (today - timedelta(days=3)).isoformat()

    def burn(p):
        p = p or {}
        if p.get("dry_run"):
            if p["date"] == missed:
                return 200, {"ok": True, "applied": [{"name": "x"}],
                             "skipped_existing": []}
            return 200, {"ok": True, "applied": [], "skipped_existing": ["x"]}
        return 200, {"ok": True, "applied": [{"name": "x"}], "skipped_existing": []}
    routes["/api/forecast/decrement-daily"] = burn
    routes["/api/inventory"] = lambda p: (200, [
        {"name": "A", "warehouse": "Ocala, FL", "weekly_usage": 7,
         "last_count_at": (today - timedelta(days=6)).isoformat()},
        {"name": "B", "warehouse": "Alcoa, TN", "weekly_usage": 7,
         "last_count_at": (today - timedelta(days=1)).isoformat() + "T10:00:00"},
        {"name": "C", "warehouse": "Idle, XX", "weekly_usage": 0},
    ])
    scheduler._run_forecast_decrement()
    real = [c[2] for c in calls if c[1] == "/api/forecast/decrement-daily"
            and not (c[2] or {}).get("dry_run")]
    assert real == [{"date": missed, "warehouse_prefix": "Ocala, FL"}, {}]
    dry = [c[2]["date"] for c in calls if (c[2] or {}).get("dry_run")]
    assert len(dry) == 7 and today.isoformat() not in dry
    f = _feeds()["forecast-burn"]
    assert f["ok"] is True and missed in f["detail"]


def test_forecast_backfill_can_be_switched_off(http, monkeypatch):
    calls, _ = http
    monkeypatch.setenv("FORECAST_BACKFILL_DAYS", "0")
    scheduler._run_forecast_decrement()
    assert [c[2] for c in calls] == [{}]


def test_forecast_failure_is_recorded(http):
    calls, routes = http
    routes["/api/forecast/decrement-daily"] = lambda p: (500, {"error": "x"})
    scheduler._run_forecast_decrement()
    assert _feeds()["forecast-burn"]["ok"] is False


def test_catch_up_runs_only_what_is_overdue(monkeypatch):
    ran = []
    monkeypatch.setattr(scheduler, "_CATCH_UP", tuple(
        (feed, (lambda f=feed: ran.append(f)), hours, even)
        for feed, _fn, hours, even in scheduler._CATCH_UP))
    # first deploy: only the two required jobs
    assert scheduler._catch_up() == ["backup", "forecast-burn"]
    now = datetime.now(timezone.utc)
    feed_health.record("backup", True)
    feed_health.record("forecast-burn", True)
    feed_health.record("graph-renew", True)
    feed_health.record("production-scan", True)
    ran.clear()
    assert scheduler._catch_up(now) == []
    assert scheduler._catch_up(now + timedelta(hours=2)) == ["backup"]
    assert scheduler._catch_up(now + timedelta(hours=26)) == [
        "backup", "forecast-burn", "graph-renew", "production-scan"]


def test_scheduler_registers_every_job(monkeypatch):
    monkeypatch.setenv("INPROCESS_SCHEDULER", "on")
    monkeypatch.setattr(scheduler, "_scheduler", None)
    sched = scheduler.start_scheduler()
    try:
        ids = {j.id for j in sched.get_jobs()}
        assert ids == {"forecast-decrement-daily", "graph-subscriptions-renew",
                       "graph-subscriptions-renew-pm", "production-scan-daily",
                       "delivery-sync-20m", "toast-nightly-pull", "backup-hourly",
                       "watchdog-hourly", "scheduler-heartbeat", "catch-up-once"}
        # gunicorn.conf.py formats every job's next run time at start-up
        assert all(f"{j.next_run_time:%H:%M %Z}" for j in sched.get_jobs())
        assert _feeds()["scheduler"]["ok"] is True
    finally:
        sched.shutdown(wait=False)
        monkeypatch.setattr(scheduler, "_scheduler", None)


def test_scheduler_off_switch(monkeypatch):
    monkeypatch.setenv("INPROCESS_SCHEDULER", "off")
    monkeypatch.setattr(scheduler, "_scheduler", None)
    assert scheduler.start_scheduler() is None
