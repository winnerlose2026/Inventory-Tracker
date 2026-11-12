"""A Lineage-held PO that is never confirmed no longer sits Pending forever
unnoticed, and the hold no longer fails open.

  * ledger rows carry held_days / stuck / stuck_reason / hold_kind;
  * suggest_absorb appears when a count taken >= 48 h after the appointment
    clearly already contains the PO, with the endpoint + payload to post;
  * POST /api/pos/absorb-pending turns the pending lines into absorbed rows
    without touching on-hand, releases the Lineage record, and a re-scan of
    the PO's email is still a replay;
  * po_health.run_checks() raises po-stuck:<po> / po-hold-unreadable;
  * a held row stays pending when po_delivery.json can't be read.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from integrations import po_delivery as D
from integrations import po_health as PH

TOKEN = "test-token-123"
H = {"X-Inventory-Token": TOKEN}
PO = "393072B2"
WH = "Houston, TX"
K1 = "plain bagel 4oz [usf - houston]"
K2 = "egg bagel 4oz [usf - houston]"
NOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INVENTORY_API_TOKEN", TOKEN)
    import inventory_tracker as it
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    for attr, fn in (("INVENTORY_FILE", "inventory.json"), ("USAGE_FILE", "usage.json"),
                     ("CANCELED_POS_FILE", "canceled_pos.json"),
                     ("CHEFS_WAREHOUSE_POS_FILE", "chefs_warehouse_pos.json"),
                     ("STATUS_OVERRIDES_FILE", "po_status_overrides.json"),
                     ("PO_DELIVERY_FILE", "po_delivery.json"),
                     ("PO_DELIVERY_THREADS_FILE", "po_delivery_threads.json"),
                     ("FREIGHT_INVOICES_FILE", "freight_invoices.json"),
                     ("PRODUCTION_FILE", "production.json"),
                     ("INVENTORY_AUDIT_FILE", "inventory_audit.json")):
        monkeypatch.setattr(it, attr, tmp_path / fn)
    import app as app_module
    app_module.app.config["TESTING"] = True
    return it, app_module.app.test_client()


def _tracking(appt_days_ago=None, requested_days_ago=20):
    rec, _, _ = D.apply_event(None, {
        "po_number": PO, "event": "requested",
        "thread": {"conversation_id": "c1", "subject": "Quote and Schedule"},
        "evidence": {"message_id": "<req>",
                     "sent": _iso(NOW - timedelta(days=requested_days_ago)),
                     "quote": f"PO #: {PO}"}}, NOW - timedelta(days=requested_days_ago))
    if appt_days_ago is not None:
        appt = NOW - timedelta(days=appt_days_ago)
        rec, _, err = D.apply_event(rec, {
            "po_number": PO, "event": "scheduled", "delivery_appt": _iso(appt),
            "evidence": {"sender": "bmorris@onelineage.com",
                         "sent": _iso(appt - timedelta(days=2)),
                         "quote": "scheduled for delivery"}},
            appt - timedelta(days=2))
        assert not err, err
    return {PO: rec}


def _inv(count_days_ago=1.0, counted=(230.0, 60.0), projected=(12.0, 6.0)):
    count_at = _iso(NOW - timedelta(days=count_days_ago))
    inv = {}
    for key, name, qty, c, p in ((K1, "Plain", 224.0, counted[0], projected[0]),
                                 (K2, "Egg", 56.0, counted[1], projected[1])):
        inv[key] = {
            "name": f"{name} Bagel 4oz [USF - Houston]", "distributor": "US Foods",
            "warehouse": WH, "unit": "cs", "quantity": c, "weekly_usage": 10.0,
            "last_count_at": count_at, "last_count_qty": c,
            "count_check": {"count_at": count_at, "projected": p},
            "on_order": [{"po_number": PO, "po_revision": "0000005", "qty": qty,
                          "unit": "cs", "ordered_at": "2026-09-28T00:00:00",
                          "eta": "2026-10-28T00:00:00",
                          "source_received_at": "2026-09-28T19:04:28Z",
                          "source_sender": "tom.foley@usfoods.com"}]}
    return inv, count_at


def _ledger_row():
    import blueprints.pos as pos
    return next(r for r in pos.build_po_ledger() if r["po_number"] == PO)


# ------------------------------------------------------------------ stuck

def test_appointment_passed_over_three_days_is_stuck(env):
    it, _ = env
    inv, _ = _inv(counted=(10.0, 5.0))
    it.save_inventory(inv)
    it.save_po_delivery(_tracking(appt_days_ago=5))
    r = _ledger_row()
    assert r["status"] != "arrived"
    assert r["held_days"] == 5 and r["stuck"] is True
    assert r["hold_kind"] == "awaiting_delivery_confirmation"
    assert "appointment passed 5 days ago" in r["stuck_reason"]
    assert r["suggest_absorb"] is None          # the count does not contain it


def test_recent_appointment_is_held_but_not_stuck(env):
    it, _ = env
    inv, _ = _inv(counted=(10.0, 5.0))
    it.save_inventory(inv)
    it.save_po_delivery(_tracking(appt_days_ago=2))
    r = _ledger_row()
    assert r["held_days"] == 2 and r["stuck"] is False and r["stuck_reason"] == ""


def test_requested_over_fourteen_days_is_stuck(env):
    it, _ = env
    inv, _ = _inv()
    it.save_inventory(inv)
    it.save_po_delivery(_tracking(requested_days_ago=20))
    r = _ledger_row()
    assert r["stuck"] and r["hold_kind"] == "awaiting_appointment" and r["held_days"] == 20


def test_no_trigger_date_and_rebooked_rows(env):
    it, _ = env
    inv, _ = _inv()
    old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%dT00:00:00")
    for item in inv.values():
        item["on_order"][0].update(eta="", ordered_at=old)
    it.save_inventory(inv)
    r = _ledger_row()
    assert r["stuck"] and r["hold_kind"] == "no_trigger_date" and r["held_days"] >= 39
    for item in inv.values():
        item["on_order"][0]["rebooked_after_count"] = (
            datetime.now() - timedelta(days=9)).strftime("%Y-%m-%d")
    it.save_inventory(inv)
    r = _ledger_row()
    assert r["stuck"] and r["hold_kind"] == "rebooked_after_count"


def test_arrived_po_is_never_stuck(env):
    it, _ = env
    it.save_inventory({K1: {"name": "Plain Bagel 4oz [USF - Houston]",
                            "warehouse": WH, "on_order": []}})
    it.save_usage([{"source": "on_order_rollover", "po_number": PO, "item_key": K1,
                    "amount": -224, "timestamp": "2026-05-31T10:00:00"}])
    r = _ledger_row()
    assert r["status"] == "arrived" and r["stuck"] is False and r["held_days"] == 0
    assert r["suggest_absorb"] is None


# ---------------------------------------------------------- suggest_absorb

def test_suggest_absorb_when_the_count_contains_the_po(env):
    it, _ = env
    inv, count_at = _inv()                 # counted 290 vs 18 projected + 280 PO
    it.save_inventory(inv)
    it.save_po_delivery(_tracking(appt_days_ago=5))
    sa = _ledger_row()["suggest_absorb"]
    assert sa["count_at"] == count_at
    assert (sa["counted"], sa["projected_without"], sa["projected_with"]) == (290.0, 18.0, 298.0)
    assert sa["endpoint"] == "/api/pos/absorb-pending"
    assert sa["payload"]["po_number"] == PO and sa["payload"]["count_at"] == count_at
    assert sa["payload"]["arrived_on"] == sa["arrived_on"]
    assert "already contains" in sa["reason"]


def test_no_suggestion_when_count_is_too_soon_or_too_low(env):
    it, _ = env
    inv, _ = _inv(count_days_ago=4.0)      # count only 24 h after the appointment
    it.save_inventory(inv)
    it.save_po_delivery(_tracking(appt_days_ago=5))
    assert _ledger_row()["suggest_absorb"] is None
    inv, _ = _inv(counted=(200.0, 40.0))   # 240 < 18 + 280 - 28
    it.save_inventory(inv)
    assert _ledger_row()["suggest_absorb"] is None


def test_absorb_pending_endpoint(env):
    it, client = env
    inv, count_at = _inv()
    it.save_inventory(inv)
    it.save_usage([])
    it.save_po_delivery(_tracking(appt_days_ago=5))
    sa = _ledger_row()["suggest_absorb"]
    # dry run by default: nothing moves
    r = client.post(sa["endpoint"], json={**sa["payload"], "dry_run": True}, headers=H)
    assert r.status_code == 200 and r.get_json()["dry_run"] is True
    assert len(it.load_inventory()[K1]["on_order"]) == 1
    # the payload as given applies it
    r = client.post(sa["endpoint"], json=sa["payload"], headers=H)
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["absorbed_cs"] == 280.0
    assert body["delivery_released"] is True
    inv2 = it.load_inventory()
    assert inv2[K1]["on_order"] == [] and inv2[K2]["on_order"] == []
    assert inv2[K1]["quantity"] == 230.0 and inv2[K2]["quantity"] == 60.0   # untouched
    rows = [u for u in it.load_usage() if u.get("source") == "on_order_absorbed"]
    assert sorted(u["absorbed_qty"] for u in rows) == [56.0, 224.0]
    assert all(u["amount"] == 0 and u["source_received_at"] == "2026-09-28T19:04:28Z"
               and u["po_revision"] == "0000005" and u["warehouse"] == WH for u in rows)
    assert it.load_po_delivery()[PO]["status"] == "released"
    row = _ledger_row()
    assert row["status"] == "arrived" and row["stuck"] is False
    # the PO's own email re-read is a replay, not a new booking
    import sync_inventory as sync
    hi, idx, rev, rcv, sender = sync._highest_applied_rev(it.load_usage(), PO)
    assert len(idx) == 2 and rcv == "2026-09-28T19:04:28Z"


def test_absorb_pending_refuses_without_support(env):
    it, client = env
    inv, count_at = _inv(counted=(10.0, 5.0))
    it.save_inventory(inv)
    it.save_po_delivery(_tracking(appt_days_ago=5))
    payload = {"po_number": PO, "count_at": count_at, "reason": "x", "dry_run": False,
               "arrived_on": (NOW - timedelta(days=5)).strftime("%Y-%m-%d")}
    r = client.post("/api/pos/absorb-pending", json=payload, headers=H)
    assert r.status_code == 409
    assert len(it.load_inventory()[K1]["on_order"]) == 1
    r = client.post("/api/pos/absorb-pending", json={**payload, "po_number": "NOPE"},
                    headers=H)
    assert r.status_code == 404
    r = client.post("/api/pos/absorb-pending", json={"po_number": PO}, headers=H)
    assert r.status_code == 400


# --------------------------------------------------------------- run_checks

def test_run_checks_raises_and_clears_po_stuck(env):
    it, _ = env
    from core import feed_health
    inv, _ = _inv()
    it.save_inventory(inv)
    it.save_po_delivery(_tracking(appt_days_ago=5))
    out = PH.run_checks()
    assert out["stuck"] == {"stuck": 1, "suggest_absorb": 1}
    iss = feed_health.snapshot()["issues"]
    assert "already contain" in iss[f"po-stuck:{PO}"]["message"]
    it.save_po_delivery(_tracking(appt_days_ago=1))
    PH.run_checks()
    assert f"po-stuck:{PO}" not in feed_health.snapshot()["issues"]


def test_run_checks_never_raises(monkeypatch):
    monkeypatch.setattr(PH, "CHECKS", [("boom", lambda now: 1 / 0)])
    assert "ZeroDivisionError" in PH.run_checks()["boom"]["error"]


# ------------------------------------------------- the hold fails CLOSED

def test_held_row_is_stamped_and_stays_pending_when_tracking_is_unreadable(env, monkeypatch):
    it, _ = env
    inv, _ = _inv(counted=(10.0, 5.0))
    for item in inv.values():                  # its date passed long ago
        item["on_order"][0]["eta"] = "2026-10-01T00:00:00"
        item["last_count_at"] = "2026-09-01"
    monkeypatch.setattr(it, "load_po_delivery", lambda: _tracking(appt_days_ago=1))
    assert it._rollover_on_order(inv) is True              # stamped
    assert all(i["on_order"][0]["lineage_hold"] is True for i in inv.values())

    def boom():
        raise it.DataFileCorrupt("po_delivery.json")
    monkeypatch.setattr(it, "load_po_delivery", boom)
    it._rollover_on_order(inv)
    assert inv[K1]["quantity"] == 10.0 and len(inv[K1]["on_order"]) == 1
    # the file came back EMPTY (lost): still held, not rolled on its date
    monkeypatch.setattr(it, "load_po_delivery", lambda: {})
    it._rollover_on_order(inv)
    assert inv[K1]["quantity"] == 10.0 and len(inv[K1]["on_order"]) == 1


def test_lazy_backfill_from_arrival_source_and_operator_wins(env, monkeypatch):
    it, _ = env
    inv, _ = _inv(counted=(10.0, 5.0))
    for item in inv.values():
        item["last_count_at"] = "2026-09-01"
        item["on_order"][0].update(arrival_date="2026-10-01T08:00:00",
                                   arrival_source="lineage")
    monkeypatch.setattr(it, "load_po_delivery", lambda: {})
    it._rollover_on_order(inv)
    assert len(inv[K1]["on_order"]) == 1                    # held, no stamp needed
    inv[K1]["on_order"][0].update(arrival_source="operator", arrival_confirmed=True)
    it._rollover_on_order(inv)
    assert inv[K1]["on_order"] == [] and inv[K1]["quantity"] == 234.0
    assert len(inv[K2]["on_order"]) == 1


def test_released_tracking_clears_the_stamp_and_untracked_rows_roll_as_before(env, monkeypatch):
    it, _ = env
    inv, _ = _inv(counted=(10.0, 5.0))
    for item in inv.values():
        item["last_count_at"] = "2026-09-01"
        item["on_order"][0]["eta"] = "2026-10-01T00:00:00"
    inv[K1]["on_order"][0]["lineage_hold"] = True
    trk = _tracking()
    trk[PO]["status"] = "released"
    monkeypatch.setattr(it, "load_po_delivery", lambda: trk)
    it._rollover_on_order(inv)
    assert inv[K1]["on_order"] == [] and inv[K1]["quantity"] == 234.0
    assert inv[K2]["on_order"] == []                         # never flagged: rolls


def test_run_checks_flags_orphaned_holds(env):
    it, _ = env
    from core import feed_health
    inv, _ = _inv()
    inv[K1]["on_order"][0]["lineage_hold"] = True
    it.save_inventory(inv)
    out = PH.run_checks()
    assert out["hold"]["orphans"] == [PO]
    assert PO in feed_health.snapshot()["issues"]["po-hold-unreadable"]["message"]
    it.save_po_delivery(_tracking(appt_days_ago=1))
    PH.run_checks()
    assert "po-hold-unreadable" not in feed_health.snapshot()["issues"]
