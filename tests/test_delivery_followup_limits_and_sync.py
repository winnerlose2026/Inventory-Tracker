"""Lineage follow-ups stop, the sync reports its own health, and what it
learns about lost POs is kept.

  * _is_open is False once the ledger says arrived (nothing pending);
  * chase_due stops after 6 asks or 21 days -> needs_human (GET + issue);
  * sync-threads: exception -> HTTP 500 + feed_health "delivery-sync";
    an unreadable thread cache is discarded; a truncated fetch is an error;
  * not_in_tracker / requested_but_arrived are cached, shown on
    GET /api/pos/delivery and raised by po_health (po-untracked:<po>).
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import inventory_tracker as it
from integrations import po_delivery as D
from integrations import po_health as PH
from test_po_delivery import (NOW, REQUEST_1012, ROJEAN, SCHEDULED_1015, _inv,
                              _requested, _routes, _synced)

DKEY = {"X-Inventory-Token": "dtok"}
TOK = {"X-Inventory-Token": "tok"}


@pytest.fixture
def health(tmp_path, monkeypatch):
    """feed_health in a temp dir (it reads it.DATA_DIR at call time)."""
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    from core import feed_health
    return feed_health


def _scheduled_past():
    rec, _, _ = D.apply_event(None, _requested(sent="2026-10-01T19:16:29Z"),
                              NOW - timedelta(days=14))
    rec, _, err = D.apply_event(rec, dict(
        SCHEDULED_1015, delivery_appt="2026-10-05T08:00:00-05:00",
        pickup_at="2026-10-02T13:00:00-04:00",
        evidence=dict(SCHEDULED_1015["evidence"], sent="2026-10-02T14:49:57Z")),
        NOW - timedelta(days=13))
    assert not err, err
    return rec


# ------------------------------------------------------------------ _is_open

def test_arrived_in_the_ledger_is_not_open_even_while_tracking_holds():
    import blueprints.delivery as B
    rec = _scheduled_past()
    assert B._is_open({"status": "in_transit"}, rec) is True
    assert B._is_open({"status": "arrived"}, rec) is False
    assert B._is_open({"status": "canceled"}, rec) is False
    assert B._is_open(None, rec) is False


def test_no_follow_up_is_sent_for_a_po_the_ledger_says_arrived(monkeypatch):
    c, store = _routes(monkeypatch, _inv(), {"393072B2": _scheduled_past()},
                       ledger_status="arrived", synced=_synced(base=False))
    out = c.post("/api/pos/delivery/send-followups", json={}, headers=DKEY).get_json()
    assert out["ok"] and out["sent"] == 0 and store["sent"] == []


# ------------------------------------------------------------- chase limits

def test_chases_stop_after_six_and_set_needs_human():
    rec = _scheduled_past()
    t = NOW - timedelta(days=9)
    for i in range(5):
        assert D.chase_due(rec, t) == "confirm_delivery"
        rec = D.record_chase(rec, "confirm_delivery", t, f"m{i}", send_state="sent")
        t += timedelta(hours=17)
    assert D.chase_due(rec, t) == "confirm_delivery" and not D.needs_human(rec, t)
    rec = D.record_chase(rec, "confirm_delivery", t, "m5", send_state="sent")
    later = t + timedelta(days=2)
    assert D.chase_due(rec, later) is None
    assert D.needs_human(rec, later) and "6 follow-ups" in D.chase_exhausted(rec, later)
    s = D.summary(rec, later)
    assert s["needs_human"] is True and s["chase_due"] is None and s["chase_count"] == 6


def test_chases_stop_after_21_days_and_restart_when_lineage_reschedules():
    rec = _scheduled_past()
    first = NOW - timedelta(days=9)
    rec = D.record_chase(rec, "confirm_delivery", first, "m0", send_state="sent")
    assert D.chase_due(rec, first + timedelta(days=20)) == "confirm_delivery"
    after = first + timedelta(days=21, hours=1)
    assert D.chase_due(rec, after) is None and D.needs_human(rec, after)
    # Lineage answers with a new appointment: the count starts again
    rec, _, err = D.apply_event(rec, {
        "po_number": "393072B2", "event": "rescheduled",
        "delivery_appt": (after + timedelta(days=1)).isoformat(),
        "evidence": {"sender": ROJEAN, "sent": after.isoformat(),
                     "quote": "rescheduled for tomorrow"}}, after)
    assert not err, err
    assert not D.needs_human(rec, after + timedelta(days=3))
    assert D.chase_due(rec, after + timedelta(days=3)) == "confirm_delivery"


def test_delivered_po_never_needs_a_human():
    rec = _scheduled_past()
    for i in range(7):
        rec = D.record_chase(rec, "confirm_delivery", NOW - timedelta(days=8 - i), f"m{i}")
    rec["status"] = "delivered"
    assert not D.needs_human(rec, NOW)


def test_needs_human_on_get_and_as_an_issue(monkeypatch, health):
    rec = _scheduled_past()
    for i in range(6):
        rec = D.record_chase(rec, "confirm_delivery",
                             NOW - timedelta(days=8) + timedelta(hours=17 * i), f"m{i}")
    c, store = _routes(monkeypatch, _inv(), {"393072B2": rec}, synced=_synced(base=False))
    out = c.get("/api/pos/delivery", headers=TOK).get_json()
    assert out["needs_human"] == ["393072B2"]
    assert out["pos"][0]["needs_human"] is True and out["pos"][0]["needs_human_reason"]
    sent = c.post("/api/pos/delivery/send-followups", json={}, headers=DKEY).get_json()
    assert sent["ok"] and sent["sent"] == 0 and store["sent"] == []
    assert PH._check_chase(NOW) == {"needs_human": ["393072B2"]}
    assert "STOPPED" in health.snapshot()["issues"]["po-chase:393072B2"]["message"]


# --------------------------------------------------------------- the sync

def _graph(monkeypatch, pages=None, fail=False):
    import blueprints.email as E
    monkeypatch.setenv("MS365_USER", "JD@ms.hhbagels.com")
    monkeypatch.setattr(E, "_graph_token", lambda: "g")
    iso = lambda h: (NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    req = {"id": "req-jd", "subject": "Quote and Schedule - 2 Orders",
           "conversationId": "conv1", "sentDateTime": iso(70),
           "internetMessageId": "<req>",
           "from": {"emailAddress": {"address": "Isaiah@hhbagels.com"}},
           "toRecipients": [], "ccRecipients": [],
           "uniqueBody": {"content": REQUEST_1012}}
    calls = {"n": 0}

    def graph(url, token, extra=None, timeout=30):
        if fail:
            raise RuntimeError("graph is down")
        calls["n"] += 1
        if pages:
            return {"value": [req] if calls["n"] == 1 else [],
                    "@odata.nextLink": url + "&p"}
        return {"value": [req]}
    monkeypatch.setattr(E, "_graph_json", graph)
    return calls


def test_sync_keeps_not_in_tracker_and_requested_but_arrived(monkeypatch, health):
    _graph(monkeypatch)
    c, store = _routes(monkeypatch, _inv(), {}, ledger_status="arrived")
    out = c.post("/api/pos/delivery/sync-threads", json={}, headers=TOK)
    body = out.get_json()
    assert out.status_code == 200 and body["ok"]
    assert body["not_in_tracker"] == ["014511763291"]
    assert body["requested_but_arrived"] == ["393072B2"]
    cache = store["threads"]
    assert [u["po_number"] for u in cache["not_in_tracker"]] == ["014511763291"]
    assert cache["not_in_tracker"][0]["address"].startswith("Cheney Brothers")
    assert cache["requested_but_arrived"][0]["po_number"] == "393072B2"
    got = c.get("/api/pos/delivery", headers=TOK).get_json()
    assert [u["po_number"] for u in got["not_in_tracker"]] == ["014511763291"]
    assert [u["po_number"] for u in got["requested_but_arrived"]] == ["393072B2"]
    assert got["synced_at"] == NOW.isoformat() and got["synced_age_minutes"] == 0.0
    assert health.snapshot()["feeds"]["delivery-sync"]["ok"] is True


def test_sync_failure_is_http_500_and_recorded(monkeypatch, health):
    _graph(monkeypatch, fail=True)
    c, store = _routes(monkeypatch, _inv(), {})
    monkeypatch.setattr(it, "load_po_delivery",
                        lambda: (_ for _ in ()).throw(RuntimeError("disk")))
    out = c.post("/api/pos/delivery/sync-threads", json={}, headers=TOK)
    assert out.status_code == 500 and out.get_json()["ok"] is False
    feed = health.snapshot()["feeds"]["delivery-sync"]
    assert feed["ok"] is False and feed["last_attempt"]


def test_unreadable_thread_cache_is_discarded_not_fatal(monkeypatch, health):
    _graph(monkeypatch)
    c, store = _routes(monkeypatch, _inv(eta="2026-10-28T00:00:00"), {})

    def boom():
        raise it.DataFileCorrupt("po_delivery_threads.json")
    monkeypatch.setattr(it, "load_po_delivery_threads", boom)
    out = c.post("/api/pos/delivery/sync-threads", json={}, headers=TOK)
    assert out.status_code == 200 and out.get_json()["registered"] == ["393072B2"]
    assert "<req>" in store["threads"]["messages"]          # rebuilt from the mailbox


def test_truncated_fetch_is_an_error_that_blocks_followups(monkeypatch, health):
    calls = _graph(monkeypatch, pages=True)
    c, store = _routes(monkeypatch, _inv(), {})
    out = c.post("/api/pos/delivery/sync-threads", json={}, headers=TOK).get_json()
    assert out["ok"] and any("truncated after 6 pages" in e for e in out["errors"])
    assert any("truncated" in e for e in store["threads"]["errors"])
    assert health.snapshot()["feeds"]["delivery-sync"]["partial"] is True
    sent = c.post("/api/pos/delivery/send-followups", json={}, headers=DKEY).get_json()
    assert sent["ok"] is False and "partial read" in sent["error"]


# ---------------------------------------------------------- lost-PO detector

def _health_env(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    for attr, fn in (("INVENTORY_FILE", "inventory.json"), ("USAGE_FILE", "usage.json"),
                     ("CANCELED_POS_FILE", "canceled_pos.json"),
                     ("CHEFS_WAREHOUSE_POS_FILE", "chefs_warehouse_pos.json"),
                     ("STATUS_OVERRIDES_FILE", "po_status_overrides.json"),
                     ("PO_DELIVERY_FILE", "po_delivery.json"),
                     ("PO_DELIVERY_THREADS_FILE", "po_delivery_threads.json"),
                     ("FREIGHT_INVOICES_FILE", "freight_invoices.json"),
                     ("PRODUCTION_FILE", "production.json")):
        monkeypatch.setattr(it, attr, tmp_path / fn)


def test_untracked_pos_from_threads_freight_and_production(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    _health_env(tmp_path, monkeypatch)
    from core import feed_health
    now = datetime.now(timezone.utc)
    day = lambda n: (now - timedelta(days=n)).strftime("%Y-%m-%d")
    it.save_inventory({"plain bagel 4oz [usf - houston]": {
        "name": "Plain Bagel 4oz [USF - Houston]", "warehouse": "Houston, TX",
        "distributor": "US Foods",
        "on_order": [{"po_number": "KNOWN1", "qty": 56, "eta": day(-10)}]}})
    it.save_canceled_pos({"CANCELED9": {"reason": "x"}})
    it.save_po_delivery_threads({"synced_at": now.isoformat(), "not_in_tracker": [
        {"po_number": "014511763291", "request_sent": day(3) + "T15:00:00Z",
         "address": "Cheney Brothers Inc, 1 Cheney Way, Riviera Beach, FL 33404",
         "quantity": "6 pallets", "subject": "Quote and Schedule - 2 Orders"},
        {"po_number": "DELI-77", "request_sent": day(3) + "T15:00:00Z",
         "address": "DeliBag, 1 Main St, Newark NJ"},                # not a tracked distributor
        {"po_number": "OLD123456", "request_sent": day(90) + "T15:00:00Z",
         "address": "US Foods, Houston, TX"},                        # too old
        {"po_number": "CANCELED9", "request_sent": day(2) + "T15:00:00Z",
         "address": "US Foods, Houston, TX"}]})                      # canceled on purpose
    it._write_json(it.FREIGHT_INVOICES_FILE, [
        {"invoice_number": "F1", "po_number": "7777777B2", "distributor": "US Foods",
         "ship_date": day(5), "dest_dc": "Houston, TX"},
        {"invoice_number": "F2", "po_number": "KNOWN1", "distributor": "US Foods",
         "ship_date": day(5), "dest_dc": "Houston, TX"}])
    it._write_json(it.PRODUCTION_FILE, [
        {"po_number": "CWNY.PO.1209205", "distributor": "Chefs Warehouse",
         "production_date": day(4), "total_cases": 56, "warehouse": "Bronx, NY"},
        {"po_number": "", "distributor": "US Foods", "production_date": day(4)}])
    found = PH.untracked_pos(now)
    assert set(found) == {"014511763291", "7777777B2", "CWNY.PO.1209205"}
    out = PH.run_checks()
    assert out["untracked"]["untracked"] == sorted(found)
    iss = feed_health.snapshot()["issues"]
    assert iss["po-untracked:014511763291"]["severity"] == "error"
    assert "freight invoice F1" in iss["po-untracked:7777777B2"]["message"]
    # booked since: the issue clears
    inv = it.load_inventory()
    inv["plain bagel 4oz [usf - houston]"]["on_order"].append(
        {"po_number": "7777777B2", "qty": 56, "eta": day(-10)})
    it.save_inventory(inv)
    PH.run_checks()
    assert "po-untracked:7777777B2" not in feed_health.snapshot()["issues"]


def test_requested_but_arrived_is_an_issue_only_when_recent(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    _health_env(tmp_path, monkeypatch)
    from core import feed_health
    now = datetime.now(timezone.utc)
    it.save_po_delivery_threads({"requested_but_arrived": [
        {"po_number": "A1", "request_sent": (now - timedelta(days=3)).isoformat(),
         "arrival_date": "2026-11-01", "arrival_kind": "eta"},
        {"po_number": "OLD", "request_sent": (now - timedelta(days=50)).isoformat()}]})
    assert PH._check_arrived_unconfirmed(now) == {"arrived_unconfirmed": ["A1"]}
    assert "po-arrived-unconfirmed:A1" in feed_health.snapshot()["issues"]
