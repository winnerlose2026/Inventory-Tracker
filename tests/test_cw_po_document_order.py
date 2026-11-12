"""Chefs Warehouse PO records are ordered by their source email, not by the
order the scan happens to read them.

Graph lists the mailbox newest first and each record overwrote the previous
one, so the OLDEST copy of a re-sent PO in the window won every scan.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import inventory_tracker
import sync_inventory as sync


def _rec(total_cs, received="", po="1226111"):
    return {"po_number": po, "warehouse": "Opa Locka, FL", "ship_to_id": "600001",
            "total_cs": total_cs, "total_usd": None,
            "lines": [{"cw_item": "10507809", "qty": total_cs, "ext_cost": None}],
            **({"source_received_at": received,
                "source_sender": "nlopez@chefswarehouse.com"} if received else {})}


@pytest.fixture
def store(monkeypatch):
    state = {"recs": [], "canceled": {}}
    monkeypatch.setattr(sync, "load_chefs_warehouse_pos", lambda: list(state["recs"]))
    monkeypatch.setattr(sync, "save_chefs_warehouse_pos",
                        lambda recs: state.update(recs=list(recs)))
    monkeypatch.setattr(inventory_tracker, "load_canceled_pos",
                        lambda: state["canceled"])
    from integrations import po_health
    monkeypatch.setattr(po_health, "flag_cw_review", lambda *a, **k: None)
    return state


def test_newest_copy_wins_when_listed_newest_first(store):
    rep = sync._apply_cw_pos([_rec(168, "2026-11-10T15:00:00Z"),     # correction
                              _rec(112, "2026-11-03T15:00:00Z")])    # original
    assert store["recs"][0]["total_cs"] == 168
    assert rep["skipped_stale"] == 1


def test_newer_copy_replaces_and_rescan_is_idempotent(store):
    sync._apply_cw_pos([_rec(112, "2026-11-03T15:00:00Z")])
    store["recs"][0]["ship_date"] = "2026-11-05"
    sync._apply_cw_pos([_rec(168, "2026-11-10T15:00:00Z")])
    assert store["recs"][0]["total_cs"] == 168
    assert store["recs"][0]["ship_date"] == "2026-11-05"      # operator field kept
    rep = sync._apply_cw_pos([_rec(168, "2026-11-10T15:00:00Z"),
                              _rec(112, "2026-11-03T15:00:00Z")])
    assert store["recs"][0]["total_cs"] == 168 and rep["updated"] == 0


def test_stored_record_without_a_stamp_counts_as_oldest(store):
    store["recs"] = [_rec(112)]                      # saved before the stamp existed
    sync._apply_cw_pos([_rec(168, "2026-11-10T15:00:00Z"),
                        _rec(112, "2026-11-03T15:00:00Z")])
    assert store["recs"][0]["total_cs"] == 168
    assert store["recs"][0]["source_received_at"] == "2026-11-10T15:00:00Z"


def test_undated_copy_cannot_replace_a_dated_one_but_two_undated_still_replace(store):
    sync._apply_cw_pos([_rec(168, "2026-11-10T15:00:00Z")])
    sync._apply_cw_pos([_rec(112)])
    assert store["recs"][0]["total_cs"] == 168
    store["recs"] = [_rec(112)]
    sync._apply_cw_pos([_rec(56)])
    assert store["recs"][0]["total_cs"] == 56


def test_internal_forward_never_beats_the_vendors_copy(store):
    sync._apply_cw_pos([_rec(168, "2026-11-10T15:00:00Z")])
    fwd = _rec(112, "2026-11-11T09:00:00Z")
    fwd["source_sender"] = "jd@hhbagels.com"
    sync._apply_cw_pos([fwd])
    assert store["recs"][0]["total_cs"] == 168


def test_canceled_match_is_normalised(store):
    store["canceled"] = {"1226111": {"reason": "x"}}
    rep = sync._apply_cw_pos([_rec(112, "2026-11-03T15:00:00Z", po="HHB-1226111.")])
    assert rep["skipped_canceled"] == 1 and store["recs"] == []
