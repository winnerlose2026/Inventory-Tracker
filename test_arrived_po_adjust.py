#!/usr/bin/env python3
"""Adjusting an ARRIVED PO in place, without a destructive Reopen.

Every other PO write path edits item["on_order"]; an arrived PO has none left,
so ship-date / po-order-date / remove-po all silently no-op on it and the only
recourse was Reopen -- which un-rolls every line and blanks the dates.

On-hand may only move when the count-absorption state changes, because a
receipt's cases are a separate addend on on-hand ONLY while no count has
superseded them. Same rule as _receipts_after_count and
_rollover_still_in_onhand; these three must never disagree.
"""
import sys
sys.path.insert(0, ".")

import blueprints.pos as pos
from app import app

KEY = "asiago bagel 4oz [usf - chicago]"
KEY2 = "plain bagel 4oz [usf - chicago]"
PO = "2753463T"


def _inv(qty=0.0, count_at="2026-08-24T12:53:56Z", qty2=34.57):
    return {
        KEY: {"name": "Asiago Bagel 4oz [USF - Chicago]", "quantity": qty,
              "unit": "cs", "warehouse": "Chicago, IL", "last_count_at": count_at},
        KEY2: {"name": "Plain Bagel 4oz [USF - Chicago]", "quantity": qty2,
               "unit": "cs", "warehouse": "Chicago, IL", "last_count_at": count_at},
    }


def _usage(arrival="2026-08-23T00:00:00"):
    return [
        {"source": "on_order_rollover", "po_number": PO, "item_key": KEY,
         "amount": -56.0, "unit": "cs", "arrival_date": arrival,
         "timestamp": "2026-08-23T04:09:30", "po_revision": "0000005"},
        {"source": "on_order_rollover", "po_number": PO, "item_key": KEY2,
         "amount": -56.0, "unit": "cs", "arrival_date": arrival,
         "timestamp": "2026-08-23T04:09:30", "po_revision": "0000005"},
    ]


def _run(monkey, body, inv=None, usage=None, freight=None):
    inv = _inv() if inv is None else inv
    usage = _usage() if usage is None else usage
    saved = {}
    monkey.setattr(pos, "load_inventory", lambda: inv)
    monkey.setattr(pos, "load_usage", lambda: usage)
    monkey.setattr(pos, "save_inventory", lambda d: saved.update(inv=d))
    monkey.setattr(pos, "save_usage", lambda d: saved.update(usage=d))
    monkey.setattr(pos, "_freight_ship_date_index", lambda: (freight or {}))
    with app.test_request_context(json=body):
        resp = pos.api_arrived_po_adjust()
    payload, status = (resp if isinstance(resp, tuple) else (resp, 200))
    return payload.get_json(), status, inv, usage


# --- arrival date moves across the count ---------------------------------

def test_arrival_moved_after_count_adds_the_cases(monkeypatch):
    """The live 2026-08-28 Chicago case: ETA said 08-23, the truck landed after
    the 08-24 08:53 count, and the counted 3 cs plainly excluded its 56."""
    out, st, inv, _ = _run(monkeypatch, {"po_number": PO,
                                         "arrival_date": "2026-08-25"})
    assert st == 200 and out["ok"]
    assert inv[KEY]["quantity"] == 56.0
    assert inv[KEY2]["quantity"] == 90.57
    assert out["onhand_delta_cs"] == 112.0
    assert "never saw these cases" in out["changes"][0]["note"]


def test_arrival_moved_before_count_subtracts(monkeypatch):
    """The other direction: the count already contains them, so a live
    rollover addend on top would be counting the delivery twice."""
    out, st, inv, _ = _run(monkeypatch, {"po_number": PO,
                                         "arrival_date": "2026-08-20"},
                           usage=_usage(arrival="2026-08-26T00:00:00"))
    assert inv[KEY]["quantity"] == -56.0     # caller's starting figure was live
    assert out["onhand_delta_cs"] == -112.0
    assert "already contains these cases" in out["changes"][0]["note"]


def test_arrival_move_within_the_same_side_moves_nothing(monkeypatch):
    """Both dates before the count -> absorption state unchanged."""
    out, st, inv, _ = _run(monkeypatch, {"po_number": PO,
                                         "arrival_date": "2026-08-21"})
    assert inv[KEY]["quantity"] == 0.0
    assert out["onhand_delta_cs"] == 0.0


def test_uncounted_sku_never_moves_on_an_arrival_edit(monkeypatch):
    """Houston has no count; the rollover is always live, so re-dating it
    changes nothing about whether its cases are on hand."""
    out, _, inv, _ = _run(monkeypatch, {"po_number": PO,
                                        "arrival_date": "2026-08-25"},
                          inv=_inv(count_at=""))
    assert out["onhand_delta_cs"] == 0.0


# --- received quantity ----------------------------------------------------

def test_short_ship_on_a_live_receipt_moves_on_hand(monkeypatch):
    out, _, inv, usage = _run(monkeypatch,
                              {"po_number": PO,
                               "lines": [{"item_key": KEY, "qty": 40}]},
                              usage=_usage(arrival="2026-08-26T00:00:00"))
    assert out["onhand_delta_cs"] == -16.0
    assert usage[0]["amount"] == -40.0
    assert usage[0]["qty_adjusted_from"] == 56.0


def test_short_ship_on_an_absorbed_receipt_is_record_only(monkeypatch):
    """The count already measured what is physically on the shelf, so the PO
    record is corrected but on-hand must not move."""
    out, _, inv, usage = _run(monkeypatch,
                              {"po_number": PO,
                               "lines": [{"item_key": KEY, "qty": 40}]})
    assert out["onhand_delta_cs"] == 0.0
    assert inv[KEY]["quantity"] == 0.0
    assert usage[0]["amount"] == -40.0
    assert out["changes"][0]["record_only"] is True


def test_arrival_and_qty_compose(monkeypatch):
    """Landed after the count AND short-shipped: on-hand gains what actually
    turned up, not what the PO said."""
    out, _, inv, _ = _run(monkeypatch, {"po_number": PO,
                                        "arrival_date": "2026-08-25",
                                        "lines": [{"item_key": KEY, "qty": 40}]})
    assert inv[KEY]["quantity"] == 40.0
    assert out["onhand_delta_cs"] == 96.0     # 40 asiago + 56 plain


def test_unknown_line_is_refused(monkeypatch):
    out, st, _, _ = _run(monkeypatch, {"po_number": PO,
                                       "lines": [{"item_key": "onion bagel 4oz [usf - chicago]",
                                                  "qty": 8}]})
    assert st == 400 and "not lines on this PO" in out["error"]


def test_negative_qty_is_refused(monkeypatch):
    out, st, _, _ = _run(monkeypatch, {"po_number": PO,
                                       "lines": [{"item_key": KEY, "qty": -5}]})
    assert st == 400


# --- ship date + the freight lock -----------------------------------------

def test_ship_date_is_record_only(monkeypatch):
    out, _, inv, usage = _run(monkeypatch, {"po_number": PO,
                                            "ship_date": "2026-08-19"})
    assert out["onhand_delta_cs"] == 0.0
    assert usage[0]["ship_date"].startswith("2026-08-19")


def test_freight_verified_ship_date_is_refused_without_override(monkeypatch):
    out, st, _, usage = _run(monkeypatch, {"po_number": PO,
                                           "ship_date": "2026-08-19"},
                             freight={PO: "2026-08-18"})
    assert st == 409 and out["freight_verified"] is True
    assert "ship_date" not in usage[0]        # nothing written


def test_freight_override_requires_a_reason(monkeypatch):
    out, st, _, _ = _run(monkeypatch, {"po_number": PO, "ship_date": "2026-08-19",
                                       "override_freight": True},
                         freight={PO: "2026-08-18"})
    assert st == 400 and "reason" in out["error"]


def test_freight_override_with_a_reason_goes_through_and_is_recorded(monkeypatch):
    out, st, _, usage = _run(monkeypatch,
                             {"po_number": PO, "ship_date": "2026-08-19",
                              "override_freight": True,
                              "reason": "Lineage invoice had the wrong PO"},
                             freight={PO: "2026-08-18"})
    assert st == 200 and out["freight_overridden"] is True
    assert usage[0]["ship_date_source"] == "operator-override"
    assert usage[0]["adjust_reason"] == "Lineage invoice had the wrong PO"


# --- guards ---------------------------------------------------------------

def test_po_with_no_arrived_lines_404s(monkeypatch):
    out, st, _, _ = _run(monkeypatch, {"po_number": "NOPE",
                                       "arrival_date": "2026-08-25"})
    assert st == 404


def test_empty_change_set_is_refused(monkeypatch):
    out, st, _, _ = _run(monkeypatch, {"po_number": PO})
    assert st == 400 and "nothing to change" in out["error"]


def test_bad_date_is_refused(monkeypatch):
    out, st, _, _ = _run(monkeypatch, {"po_number": PO, "arrival_date": "25/08/2026"})
    assert st == 400


def test_reversed_rows_are_ignored(monkeypatch):
    u = _usage()
    u[0]["reversed"] = True
    out, _, inv, _ = _run(monkeypatch, {"po_number": PO,
                                        "arrival_date": "2026-08-25"}, usage=u)
    assert out["lines_touched"] == 1
    assert inv[KEY]["quantity"] == 0.0          # the reversed line stays put
    assert inv[KEY2]["quantity"] == 90.57


def test_an_audit_row_is_written_for_every_move(monkeypatch):
    out, _, _, usage = _run(monkeypatch, {"po_number": PO,
                                          "arrival_date": "2026-08-25",
                                          "reason": "JD confirmed receipt"})
    audit = [e for e in usage if e.get("source") == "arrived-po-adjust"]
    assert len(audit) == 2
    assert audit[0]["amount"] == -56.0          # negative == stock added
    assert "JD confirmed receipt" in audit[0]["note"]
