"""Activity Log + Pending POs <-> Inventory flow check (blueprints/activity.py,
core/activity.py)."""
from __future__ import annotations

from datetime import datetime

import pytest

from test_po_operator_paths import H, KEY, _book, _day, env  # noqa: F401


@pytest.fixture
def logged(env, monkeypatch):
    it, sync, c = env
    from app import app
    monkeypatch.setitem(app.config, "ACTIVITY_LOG", True)
    return it, sync, c


# --- flow check ---------------------------------------------------------------

def test_flow_check_is_clean_when_tabs_agree(env):
    it, sync, c = env
    _book(sync, "7700001O", order_day=_day(-3))
    r = c.get("/api/activity/flow-check", headers=H).get_json()
    assert r["ok"] and r["issue_count"] == 0
    man = next(w for w in r["warehouses"] if w["warehouse"] == "Manassas, VA")
    assert man["open_pos"] == 1 and man["pending_pos_cs"] == man["inventory_on_order_cs"] == 56
    assert man["ok"]


def test_flow_check_flags_the_la_mirada_split(env):
    """Rolled into on-hand on the ETA, then pinned In Production -- the exact
    production state of 7135734C before the fix moved it back."""
    it, sync, c = env
    _book(sync, "7135734C", order_day=_day(-40))
    it.load_inventory()                                     # rolls it over
    it.save_status_overrides({"7135734C": "in_production"})
    usage = it.load_usage()
    for e in usage:
        if e.get("source") == "on_order_rollover":
            e["arrival_date"] = f"{_day(14)}T00:00:00"
    it.save_usage(usage)
    r = c.get("/api/activity/flow-check", headers=H).get_json()
    kinds = {i["kind"] for i in r["issues"]}
    assert not r["ok"]
    assert {"open_but_in_on_hand", "arrived_future_date"} <= kinds
    man = next(w for w in r["warehouses"] if w["warehouse"] == "Manassas, VA")
    assert man["ok"] is False and man["inventory_on_order_cs"] == 0
    # ...and setting the status again repairs it
    c.post("/api/pending/set-status", json={"po_number": "7135734C",
                                            "status": "in_production"}, headers=H)
    assert c.get("/api/activity/flow-check", headers=H).get_json()["ok"]


def test_flow_check_flags_arrived_label_on_a_po_still_on_order(env):
    it, sync, c = env
    _book(sync, "7700002O", order_day=_day(-3))
    c.post("/api/pending/set-status", json={"po_number": "7700002O",
                                            "status": "arrived"}, headers=H)
    r = c.get("/api/activity/flow-check", headers=H).get_json()
    assert [i["kind"] for i in r["issues"]] == ["arrived_but_on_order"]


def test_flow_check_notes_a_status_hold_past_its_eta(env):
    it, sync, c = env
    _book(sync, "7700003O", order_day=_day(-25))
    c.post("/api/pending/set-status", json={"po_number": "7700003O",
                                            "status": "in_production"}, headers=H)
    inv = it.load_inventory()
    inv[KEY]["on_order"][0]["eta"] = f"{_day(-1)}T00:00:00"
    it.save_inventory(inv)
    r = c.get("/api/activity/flow-check", headers=H).get_json()
    assert r["ok"] and r["notes"][0]["kind"] == "held_by_status"


def test_scan_health_carries_the_flow_check(env):
    it, sync, c = env
    r = c.get("/api/scan/health", headers=H).get_json()
    assert r["po_flow"]["ok"] is True


def test_cw_pos_are_reported_separately(env):
    it, sync, c = env
    it.save_chefs_warehouse_pos([{"po_number": "1226111", "warehouse": "Opa Locka, FL",
                                  "ship_to_id": "600001", "dc_code": "MD",
                                  "total_cs": 112, "lines": []}])
    r = c.get("/api/activity/flow-check", headers=H).get_json()
    assert r["ok"]
    assert r["chefs_warehouse"] == [{"warehouse": "Opa Locka, FL", "dc_code": "FLA",
                                     "open_pos": 1, "open_cs": 112.0}]


# --- action log ----------------------------------------------------------------

def test_every_change_request_is_logged_with_its_result(logged):
    it, sync, c = logged
    _book(sync, "7700004O", order_day=_day(-40))
    it.load_inventory()
    c.post("/api/pending/set-status", json={"po_number": "7700004O",
                                            "status": "in_production"}, headers=H)
    c.post("/api/pending/set-status", json={"po_number": "", "status": "open"},
           headers=H)                                           # a 400
    from core import activity
    rows = activity.load()
    assert rows[0]["status"] == 400 and rows[0]["label"] == "PO status set"
    ok = rows[1]
    assert ok["po_number"] == "7700004O" and ok["request"]["status"] == "in_production"
    assert ok["result"]["moved_back_cs"] == 56
    assert ok["actor"] in ("API token", "cron / script")


def test_dry_runs_and_reads_are_not_logged(logged):
    it, sync, c = logged
    c.get("/api/pos/ledger", headers=H)
    c.post("/api/admin/restore-absorbed-po", json={"po_number": "X", "count_at": "y",
                                                   "dry_run": True}, headers=H)
    from core import activity
    assert activity.load() == []


def test_log_survives_an_unwritable_dir(logged, monkeypatch):
    from core import activity
    monkeypatch.setattr(activity, "_log_path",
                        lambda: __import__("pathlib").Path("/proc/nope/activity.jsonl"))
    activity.record({"ts": "x"})                                # must not raise


# --- merged timeline ---------------------------------------------------------

def test_timeline_shows_the_whole_story_of_a_po(logged):
    it, sync, c = logged
    _book(sync, "7700005O", order_day=_day(-40))
    it.load_inventory()                                         # auto-rolls
    c.post("/api/pending/set-status", json={"po_number": "7700005O",
                                            "status": "in_production"}, headers=H)
    r = c.get("/api/activity?po=7700005O&days=3", headers=H).get_json()
    labels = [e["label"] for e in r["events"]]
    assert "PO status set" in labels
    assert any(l.startswith("PO arrived") for l in labels)
    assert "Moved back on order / reversed" in labels
    arr = next(e for e in r["events"] if e["label"].startswith("PO arrived"))
    assert arr["warehouse"] == "Manassas, VA" and arr["delta_cs"] == 56
    assert "later moved back on order" in arr["label"]
    back = next(e for e in r["events"] if e["label"].startswith("Moved back"))
    assert back["delta_cs"] == -56
    act = next(e for e in r["events"] if e["label"] == "PO status set")
    assert act["warehouse"] == "Manassas, VA" and act["delta_cs"] == -56


def test_timeline_groups_a_count_and_filters(env):
    it, sync, c = env
    now = datetime.now().isoformat()
    it.append_inventory_audit([
        {"ts": now, "source": "ms365", "name": "Plain Bagel 4oz [USF - Manassas]",
         "warehouse": "Manassas, VA", "event_type": "on_hand", "old_quantity": 10,
         "new_quantity": 12, "delta": 2, "subject": "MANASSAS INVENTORY"},
        {"ts": now, "source": "ms365", "name": "Everything Bagel 4oz [USF - Manassas]",
         "warehouse": "Manassas, VA", "event_type": "on_hand", "old_quantity": 5,
         "new_quantity": 4, "delta": -1, "subject": "MANASSAS INVENTORY"},
        {"ts": now, "source": "ms365", "name": "Plain Bagel 4oz [USF - Chicago]",
         "warehouse": "Chicago, IL", "event_type": "on_order", "old_quantity": 0,
         "new_quantity": 0, "delta": 0, "po_number": "9314683Y",
         "on_order_delta": 56},
    ])
    r = c.get("/api/activity?days=1", headers=H).get_json()
    cnt = next(e for e in r["events"] if e["category"] == "count")
    assert cnt["item_count"] == 2 and cnt["delta_cs"] == 1
    assert cnt["items"][0]["from"] == 10 and "MANASSAS INVENTORY" in cnt["detail"]
    po = next(e for e in r["events"] if e["category"] == "po")
    assert po["po_number"] == "9314683Y" and po["on_order_cs"] == 56
    only = c.get("/api/activity?days=1&warehouse=Chicago,%20IL", headers=H).get_json()
    assert [e["warehouse"] for e in only["events"]] == ["Chicago, IL"]
    burn = c.get("/api/activity?days=1&category=count", headers=H).get_json()
    assert {e["category"] for e in burn["events"]} == {"count"}
