"""Feed staleness, watchdog checks and the alert email."""
from datetime import datetime, timedelta, timezone

import pytest

import inventory_tracker as it
from core import feed_health
from integrations import watchdog

TOKEN = "tok"
H = {"X-Inventory-Token": TOKEN}
NOW = datetime(2026, 11, 12, 15, 40, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _feed(name, ok=True, age_h=0.1, now=NOW):
    rec = feed_health._load()
    ts = _iso(now - timedelta(hours=age_h))
    rec["feeds"][name] = {"ok": ok, "last_attempt": ts, "detail": "d",
                          "last_ok": ts if ok else ""}
    feed_health._save(rec)


def _all_green(now=NOW):
    for n in ("mailbox-scan", "forecast-burn", "backup"):
        _feed(n, now=now)


@pytest.fixture(autouse=True)
def _roomy_disk(monkeypatch):
    import collections
    DU = collections.namedtuple("DU", "total used free")
    monkeypatch.setattr(watchdog.shutil, "disk_usage",
                        lambda p: DU(1000 * 2**20, 100 * 2**20, 900 * 2**20))


@pytest.fixture
def sent(monkeypatch):
    box = []
    monkeypatch.setattr(watchdog, "_send_email",
                        lambda subject, body: (box.append((subject, body)) or
                                               (True, "sent")))
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)
    return box


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKEN", TOKEN)
    monkeypatch.delenv("INVENTORY_API_TOKENS", raising=False)
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


# --- feeds view -------------------------------------------------------------

def test_first_deploy_lists_only_the_required_feeds_as_stale():
    v = watchdog.feeds_view(NOW)
    assert set(v["feeds"]) == {"mailbox-scan", "forecast-burn", "backup"}
    assert all(f["stale"] and not f["ok"] for f in v["feeds"].values())
    assert v["ok"] is False and v["issues"] == []
    assert set(v["feeds"]["backup"]) == {"ok", "last_ok", "last_attempt",
                                        "detail", "stale", "max_age_hours"}


def test_feed_goes_stale_past_its_max_age_and_unknown_feeds_never_do():
    _all_green()
    _feed("production-scan", age_h=27)
    _feed("backup-offsite", age_h=29)
    _feed("someone-elses-feed", age_h=999)
    v = watchdog.feeds_view(NOW)
    assert v["feeds"]["production-scan"]["stale"] is True
    assert v["feeds"]["backup-offsite"]["stale"] is False
    assert v["feeds"]["someone-elses-feed"] == {
        "ok": True, "last_ok": v["feeds"]["someone-elses-feed"]["last_ok"],
        "last_attempt": v["feeds"]["someone-elses-feed"]["last_attempt"],
        "detail": "d", "stale": False, "max_age_hours": None}
    assert v["ok"] is False


def test_delivery_sync_is_only_stale_inside_its_window():
    _all_green()
    idle = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
    _feed("delivery-sync", age_h=4.2, now=idle)              # last ran 03:47
    assert watchdog.feeds_view(idle)["feeds"]["delivery-sync"]["stale"] is False
    just_open = datetime(2026, 11, 12, 10, 30, tzinfo=timezone.utc)
    assert watchdog.feeds_view(just_open)["feeds"]["delivery-sync"]["stale"] is False
    midday = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
    assert watchdog.feeds_view(midday)["feeds"]["delivery-sync"]["stale"] is True


def test_mailbox_scan_feed_is_read_from_scan_health():
    ts = datetime.now() - timedelta(hours=2)
    it.save_scan_health({"ts": ts.isoformat(), "messages_seen": 4,
                         "messages_parsed": 3, "mailbox_failures": []})
    f = watchdog.feeds_view()["feeds"]["mailbox-scan"]
    assert f["ok"] and not f["stale"] and "4 message" in f["detail"]
    it.save_scan_health({"ts": (datetime.now() - timedelta(hours=14)).isoformat()})
    assert watchdog.feeds_view()["feeds"]["mailbox-scan"]["stale"] is True
    it.save_scan_health({"ts": datetime.now().isoformat(),
                         "mailbox_failures": ["orders@: 401"]})
    f = watchdog.feeds_view()["feeds"]["mailbox-scan"]
    assert f["ok"] is False and "401" in f["detail"]


def test_feeds_health_endpoint_contract(client, monkeypatch):
    _all_green(datetime.now(timezone.utc))
    feed_health.raise_issue("po:1", "PO 1 is stuck", "warn")
    assert client.get("/api/feeds/health").status_code == 401
    d = client.get("/api/feeds/health", headers=H).get_json()
    assert set(d) == {"ok", "generated_at", "feeds", "issues"}
    assert d["ok"] is False and d["feeds"]["backup"]["stale"] is False
    assert set(d["issues"][0]) == {"key", "message", "severity",
                                   "first_seen", "last_seen"}
    feed_health.clear_issue("po:1")
    assert client.get("/api/feeds/health", headers=H).get_json()["ok"] is True
    monkeypatch.setenv("INVENTORY_API_TOKENS", "offsite:bbb:backup")
    assert client.get("/api/feeds/health",
                      headers={"X-Inventory-Token": "bbb"}).status_code == 200


# --- checks -----------------------------------------------------------------

def test_green_system_sends_nothing(sent):
    _all_green()
    res = watchdog.run(NOW)
    assert res["ok"] and res["issues"] == [] and sent == []
    assert feed_health.snapshot()["feeds"]["watchdog"]["ok"] is True


def test_stale_feed_emails_once_then_stays_quiet_until_the_digest(sent):
    _feed("mailbox-scan", age_h=14); _feed("forecast-burn"); _feed("backup")
    watchdog.run(NOW)
    assert len(sent) == 1
    subject, body = sent[0]
    assert subject == "Inventory Tracker: 1 thing(s) need a look"
    assert "mailbox scan" in body and "bagel-inventory.onrender.com" in body
    watchdog.run(NOW + timedelta(hours=1))
    assert len(sent) == 1                                   # already told
    morning = datetime(2026, 11, 13, 13, 40, tzinfo=timezone.utc)
    _feed("forecast-burn", now=morning); _feed("backup", now=morning)
    watchdog.run(morning)
    assert len(sent) == 2 and sent[1][1].startswith("Daily summary")
    watchdog.run(morning + timedelta(minutes=5))
    assert len(sent) == 2                                   # one digest a day
    # fixed -> issue clears, nothing more is sent
    later = morning + timedelta(hours=1)
    for n in ("mailbox-scan", "forecast-burn", "backup"):
        _feed(n, now=later)
    res = watchdog.run(later)
    assert res["issues"] == [] and len(sent) == 2


def test_flapping_issue_emails_at_most_once_per_24h(sent):
    _all_green()
    feed_health.raise_issue("po:9", "PO 9 is stuck")
    watchdog.run(NOW)
    feed_health.clear_issue("po:9")
    feed_health.raise_issue("po:9", "PO 9 is stuck")        # back again
    for n in ("mailbox-scan", "forecast-burn", "backup"):
        _feed(n, now=NOW + timedelta(hours=2))
    watchdog.run(NOW + timedelta(hours=2))
    assert len(sent) == 1
    assert feed_health.new_issues() == {}
    feed_health.clear_issue("po:9")
    feed_health.raise_issue("po:9", "PO 9 is stuck")
    t = NOW + timedelta(hours=25)
    for n in ("mailbox-scan", "forecast-burn", "backup"):
        _feed(n, now=t)
    watchdog.run(t, digest=False)
    assert len(sent) == 2


def test_failed_send_keeps_issues_unnotified_and_marks_watchdog_failed(monkeypatch):
    _all_green()
    feed_health.raise_issue("po:9", "PO 9 is stuck")
    monkeypatch.setattr(watchdog, "_send_email", lambda s, b: (False, "graph 403"))
    res = watchdog.run(NOW)
    assert res["ok"] is False
    assert "po:9" in feed_health.new_issues()
    w = feed_health.snapshot()["feeds"]["watchdog"]
    assert w["ok"] is False and "graph 403" in w["detail"]
    box = []
    monkeypatch.setattr(watchdog, "_send_email",
                        lambda s, b: (box.append(s) or (True, "sent")))
    for n in ("mailbox-scan", "forecast-burn", "backup"):
        _feed(n, now=NOW + timedelta(hours=1))
    watchdog.run(NOW + timedelta(hours=1))
    assert len(box) == 1 and feed_health.new_issues() == {}


def test_alerts_off_sends_nothing_and_leaves_issues_new(sent, monkeypatch):
    _all_green()
    monkeypatch.setenv("ALERTS_ENABLED", "off")
    feed_health.raise_issue("po:9", "PO 9 is stuck")
    watchdog.run(NOW)
    assert sent == [] and "po:9" in feed_health.new_issues()


def test_unparsed_entry_older_than_6h_and_po_failures_raise_issues(sent):
    _all_green()
    it._write_json(it.UNPARSED_REPORTS_FILE, [
        {"id": "m1", "sender": "rep@usfoods.com", "subject": "Weekly inventory",
         "received": _iso(NOW - timedelta(hours=7))},
        {"id": "m2", "sender": "rep@usfoods.com", "subject": "fresh",
         "received": _iso(NOW - timedelta(hours=1))}])
    it.save_scan_health({"ts": datetime.now().isoformat(),
                         "po_failures": ["PO 7500000O: no line items parsed"]})
    res = watchdog.run(NOW)
    keys = res["issues"]
    assert sum(k.startswith("unparsed:") for k in keys) == 1
    assert sum(k.startswith("scan-po-failure:") for k in keys) == 1
    assert "Weekly inventory" in sent[0][1] and "7500000O" in sent[0][1]
    it._write_json(it.UNPARSED_REPORTS_FILE, [])
    it.save_scan_health({"ts": datetime.now().isoformat(), "po_failures": []})
    assert watchdog.run(NOW + timedelta(minutes=1))["issues"] == []


def test_warehouse_without_a_count_for_over_8_days(sent):
    _all_green()
    old = (datetime.now() - timedelta(days=9)).isoformat()
    new = (datetime.now() - timedelta(days=2)).isoformat()
    it.save_inventory({
        "a": {"name": "A", "quantity": 1, "distributor": "US Foods",
              "warehouse": "Alcoa, TN", "last_count_at": old},
        "b": {"name": "B", "quantity": 1, "distributor": "US Foods",
              "warehouse": "Zebulon, NC", "last_count_at": new},
        "c": {"name": "C", "quantity": 1, "warehouse": "Never, XX"},
        "d": {"name": "D", "quantity": 1, "warehouse": "Hidden, XX",
              "last_count_at": old, "hidden": True}})
    res = watchdog.run(NOW)
    assert [k for k in res["issues"] if k.startswith("warehouse-stale:")] == [
        "warehouse-stale:Alcoa, TN"]


def test_disk_low_issue(sent, monkeypatch):
    _all_green()
    import collections
    DU = collections.namedtuple("DU", "total used free")
    monkeypatch.setattr(watchdog.shutil, "disk_usage",
                        lambda p: DU(1000 * 2**20, 900 * 2**20, 100 * 2**20))
    assert "disk-low" in watchdog.run(NOW)["issues"]


def test_optional_hooks_are_called_and_may_fail(sent, monkeypatch):
    import sys
    import types
    _all_green()
    calls = []
    good = types.ModuleType("integrations.ingest_health")
    good.run_checks = lambda: calls.append("ingest")
    bad = types.ModuleType("integrations.po_health")
    bad.run_checks = lambda: 1 / 0
    monkeypatch.setitem(sys.modules, "integrations.ingest_health", good)
    monkeypatch.setitem(sys.modules, "integrations.po_health", bad)
    res = watchdog.run(NOW)
    assert calls == ["ingest"]
    assert res["hooks"]["integrations.ingest_health"] == "ok"
    assert res["hooks"]["integrations.po_health"].startswith("failed")


# --- books ------------------------------------------------------------------

def _books_fixture(qty):
    it.save_inventory({"plain": {
        "name": "Plain", "quantity": qty, "weekly_usage": 7,
        "last_count_at": "2026-11-09T12:00:00", "last_count_qty": 100}})
    it.save_usage([
        {"item_key": "plain", "amount": -56, "source": "on_order_rollover",
         "arrival_date": "2026-11-10T00:00:00", "po_number": "P1",
         "timestamp": "2026-11-10T06:00:00"},
        {"item_key": "plain", "amount": 1.0, "source": "forecast-daily",
         "forecast_date": "2026-11-10", "timestamp": "2026-11-10T05:05:00"},
        {"item_key": "plain", "amount": 1.0, "source": "forecast-daily",
         "forecast_date": "2026-11-11", "timestamp": "2026-11-11T05:05:00"},
        {"item_key": "plain", "amount": 1.0, "source": "forecast-daily",
         "forecast_date": "2026-11-09", "timestamp": "2026-11-09T05:05:00"},
    ])


def test_books_balance_when_on_hand_is_count_plus_receipts_minus_burn():
    _books_fixture(154)                       # 100 + 56 - 2
    assert watchdog.books_check() == []


def test_books_mismatch_raises_a_capped_issue_at_the_morning_run(sent):
    _all_green(datetime(2026, 11, 12, 7, 40, tzinfo=timezone.utc))
    _books_fixture(98)
    rows = watchdog.books_check()
    assert rows[0]["expected"] == 154 and rows[0]["gap"] == -56
    res = watchdog.run(NOW)                   # 15:40 -- not the books run
    assert "books:plain" not in res["issues"]
    res = watchdog.run(datetime(2026, 11, 12, 7, 40, tzinfo=timezone.utc))
    assert "books:plain" in res["issues"] and res["books_checked"]
    assert "Plain shows 98 cs" in sent[-1][1]


def test_watchdog_endpoint(client, sent):
    _all_green(datetime.now(timezone.utc))
    assert client.post("/api/watchdog/run", json={}).status_code == 401
    r = client.post("/api/watchdog/run", json={"send": False, "books": True},
                    headers=H).get_json()
    assert r["ok"] and r["books_checked"] is True and sent == []


def test_alert_email_goes_through_the_send_route_policy(client, monkeypatch):
    monkeypatch.setenv("ALERT_EMAIL_TO", "someone@elsewhere.example")
    import app as app_mod
    with app_mod.app.app_context():
        ok, detail = watchdog._send_email("s", "b")
    assert ok is False and "not allowed" in detail
