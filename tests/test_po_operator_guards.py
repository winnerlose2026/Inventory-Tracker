"""Operator PO edits that used to move stock silently.

  * set-status "cancelled" on a PO with pending lines was a label only -- the
    lines still rolled into on-hand. It is refused, pointing at remove-po.
  * ship / arrival dates more than 60 days past, 180 days ahead, or with the
    arrival before the ship date need force=true.
  * ship-date matches the PO number normalised and answers 404 when nothing
    matched.
  * Reopen clamps on-hand at zero: the audit row records what actually came
    out, with a warning.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_po_operator_paths import H, KEY, _book, _day, env  # noqa: F401

PO = "7500000O"


def test_cancelled_status_is_refused_while_lines_are_on_order(env):
    it, sync, c = env
    _book(sync, PO)
    it_inv = it.load_inventory()
    assert it_inv[KEY]["on_order"]
    r = c.post("/api/pending/set-status", json={"po_number": PO, "status": "cancelled"},
               headers=H)
    body = r.get_json()
    assert r.status_code == 400 and body["use_endpoint"] == "/api/admin/remove-po"
    assert body["pending_cs"] == 56
    assert it.load_status_overrides() == {}                 # no label left behind
    assert it.load_inventory()[KEY]["on_order"]             # nothing removed
    # the real cancel removes the lines; the label is then harmless
    assert c.post("/api/admin/remove-po", json={"po_number": PO, "reason": "t"},
                  headers=H).get_json()["removed_entries"] == 1
    r = c.post("/api/pending/set-status", json={"po_number": PO, "status": "cancelled"},
               headers=H)
    assert r.status_code == 200 and r.get_json()["ok"]


def test_cancelled_label_still_allowed_for_a_po_with_nothing_on_order(env):
    it, sync, c = env
    r = c.post("/api/pending/set-status", json={"po_number": "CW-1", "status": "cancelled"},
               headers=H)
    assert r.status_code == 200


def test_implausible_ship_dates_need_force(env):
    it, sync, c = env
    _book(sync, PO)
    post = lambda **b: c.post("/api/on-order/ship-date", json={"po_number": PO, **b},
                              headers=H)
    r = post(ship_date=_day(-400))                          # last year's date
    assert r.status_code == 400 and r.get_json()["needs_force"] is True
    assert not it.load_inventory()[KEY]["on_order"][0].get("ship_date")
    assert post(ship_date=_day(200)).status_code == 400
    r = post(ship_date=_day(3), arrival_date=_day(1))       # arrives before it ships
    assert r.status_code == 400 and "before ship_date" in r.get_json()["error"]
    assert post(ship_date=_day(3), arrival_date=_day(1), force=True).status_code == 200
    assert post(ship_date=_day(2)).status_code == 200
    r = c.post("/api/pending/set-status", headers=H, json={
        "po_number": PO, "status": "arrived", "arrival_date": _day(-400)})
    assert r.status_code == 400 and r.get_json()["needs_force"] is True


def test_ship_date_matches_normalised_and_404s_when_nothing_matches(env):
    it, sync, c = env
    _book(sync, PO)
    r = c.post("/api/on-order/ship-date", headers=H,
               json={"po_number": "hhb-7500000o.", "ship_date": _day(2)})
    assert r.status_code == 200 and r.get_json()["entries_updated"] == 1
    assert it.load_inventory()[KEY]["on_order"][0]["ship_date"].startswith(_day(2))
    r = c.post("/api/on-order/ship-date", headers=H,
               json={"po_number": "NOSUCHPO", "ship_date": _day(2)})
    assert r.status_code == 404 and r.get_json()["ok"] is False
    assert r.get_json()["entries_updated"] == 0


def test_unroll_records_what_actually_came_out_when_onhand_clamps(env):
    it, sync, c = env
    _book(sync, PO)
    c.post("/api/pending/set-status", headers=H, json={
        "po_number": PO, "status": "arrived", "arrival_date": _day(-1)})
    inv = it.load_inventory()
    assert not inv[KEY]["on_order"]
    inv[KEY]["quantity"] = 20.0                 # most of the 56 already used
    inv[KEY]["last_count_at"] = ""
    it.save_inventory(inv)
    r = c.post("/api/pending/reopen", json={"po_number": PO}, headers=H).get_json()
    assert r["ok"] and r["removed_cs"] == 20
    assert r["warnings"] and "only 20 of 56" in r["warnings"][0]
    assert it.load_inventory()[KEY]["quantity"] == 0.0
    row = [u for u in it.load_usage() if u.get("source") == "reversal"][-1]
    assert row["amount"] == 20 and row["clamped_at_zero"] is True
    assert row["requested_amount"] == 56 and "only 20 of 56" in row["note"]
