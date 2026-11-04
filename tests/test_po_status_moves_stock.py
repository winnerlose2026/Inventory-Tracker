"""A Pending-PO status that says "not arrived" keeps the PO on order.

2026-10-22, La Mirada 7135734C (336 cs, ordered 9/22): no ship date yet, so it
rolled into on-hand on its automatic 30-day ETA. Someone then marked it In
Production with ship 10/30 / arrival 11/06 -- but the status was display-only
and the arrived-PO editor only moves stock when an arrival crosses a COUNT, so
the Pending POs tab said In Production while the Inventory tab showed nothing
on order and on-hand carried 336 cs that had not shipped.

Now:
  * set-status Open / Overdue / In Production / In Transit moves an arrived PO
    back on order (dates kept, cases out of on-hand unless a count superseded
    them);
  * a not-arrived status holds a PO whose only trigger is the 30-day ETA;
  * the arrived-PO editor treats a future ship/arrival date as "not arrived";
  * Arrived + an arrival date rolls the PO in under the count rule;
  * a pinned not-arrived status is cleared once the PO really arrives.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from test_po_operator_paths import H, KEY, _book, _day, _ledger, env  # noqa: F401


def _rolled(it, sync, po="7135734C", qty=72, order_days_ago=40):
    """A PO auto-rolled on its 30-day ETA (no ship date), never counted."""
    _book(sync, po, qty=qty, order_day=_day(-order_days_ago))
    inv = it.load_inventory()                       # the read rolls it over
    assert not inv[KEY]["on_order"]
    return inv[KEY]["quantity"]


def test_in_production_moves_an_auto_rolled_po_back_on_order(env):
    it, sync, c = env
    q_rolled = _rolled(it, sync)
    assert _ledger("7135734C")["status"] == "arrived"
    r = c.post("/api/pending/set-status", json={"po_number": "7135734C",
                                                "status": "in_production"},
               headers=H).get_json()
    assert r["ok"] and r["moved_back_on_order"]["cs"] == 72
    assert r["moved_back_on_order"]["onhand_removed_cs"] == 72
    for _ in range(2):                              # and it STAYS there
        inv = it.load_inventory()
        assert [o["po_number"] for o in inv[KEY]["on_order"]] == ["7135734C"]
        assert inv[KEY]["quantity"] == q_rolled - 72
    rec = _ledger("7135734C")
    assert rec["status"] == "in_production" and rec["source_kind"] == "inventory"
    # the un-roll is on the record
    rows = [e for e in it.load_usage() if e.get("source") == "reversal"]
    assert rows and rows[-1]["po_number"] == "7135734C" and rows[-1]["amount"] == 72


def test_the_live_la_mirada_state_is_repaired_with_its_dates(env):
    """Exactly what production holds: rolled rows carrying the operator's
    ship 10/30 / arrival 11/06 (typed through the arrived-PO editor)."""
    it, sync, c = env
    q_rolled = _rolled(it, sync)
    usage = it.load_usage()
    for e in usage:
        if e.get("source") == "on_order_rollover":
            e.update(ship_date=f"{_day(7)}T00:00:00", ship_date_source="operator",
                     arrival_date=f"{_day(14)}T00:00:00",
                     adjusted_at=datetime.now().isoformat())
    it.save_usage(usage)
    it.save_status_overrides({"7135734C": "in_production"})
    assert _ledger("7135734C")["source_kind"] == "arrived"      # the split

    c.post("/api/pending/set-status", json={"po_number": "7135734C",
                                            "status": "in_production"}, headers=H)
    e = it.load_inventory()[KEY]["on_order"][0]
    assert e["ship_date"].startswith(_day(7))
    assert e["arrival_date"].startswith(_day(14)) and e["arrival_source"] == "operator"
    assert it.load_inventory()[KEY]["quantity"] == q_rolled - 72
    rec = _ledger("7135734C")
    assert rec["status"] == "in_production" and rec["arrival_date"].startswith(_day(14))


def test_status_hold_keeps_a_po_past_its_30_day_eta_on_order(env):
    it, sync, c = env
    _book(sync, "7600001O", order_day=_day(-25))           # ETA in 5 days
    c.post("/api/pending/set-status", json={"po_number": "7600001O",
                                            "status": "in_production"}, headers=H)
    inv = it.load_inventory()
    inv[KEY]["on_order"][0]["eta"] = f"{_day(-1)}T00:00:00"  # ETA passes
    it.save_inventory(inv)
    assert it.load_inventory()[KEY]["on_order"], "rolled on the bare ETA"
    # a real date takes over again: ship + transit in the past rolls it
    c.post("/api/on-order/ship-date", json={"po_number": "7600001O",
                                            "ship_date": _day(-9),
                                            "arrival_date": _day(-2)}, headers=H)
    assert not it.load_inventory()[KEY]["on_order"]
    rec = _ledger("7600001O")
    assert rec["status"] == "arrived" and rec["override"] == ""
    assert "7600001O" not in it.load_status_overrides()     # stale pin cleared


def test_auto_clears_the_hold(env):
    it, sync, c = env
    _book(sync, "7600002O", order_day=_day(-25))
    c.post("/api/pending/set-status", json={"po_number": "7600002O",
                                            "status": "open"}, headers=H)
    inv = it.load_inventory()
    inv[KEY]["on_order"][0]["eta"] = f"{_day(-1)}T00:00:00"
    it.save_inventory(inv)
    assert it.load_inventory()[KEY]["on_order"]
    c.post("/api/pending/set-status", json={"po_number": "7600002O", "status": ""},
           headers=H)
    assert not it.load_inventory()[KEY]["on_order"]


def test_count_superseded_rollover_comes_back_without_touching_on_hand(env):
    """The ETA passed, then a count landed: the count is what is on the
    shelf. Marking the PO In Transit puts it on order, on-hand untouched."""
    it, sync, c = env
    _book(sync, "7600003O", order_day=_day(-40))
    inv = it.load_inventory()
    inv[KEY].update(quantity=40.0, last_count_at=f"{_day(-1)}T12:00:00",
                    last_count_qty=40.0)
    it.save_inventory(inv)
    r = c.post("/api/pending/set-status", json={"po_number": "7600003O",
                                                "status": "in_transit"},
               headers=H).get_json()
    assert r["moved_back_on_order"]["onhand_removed_cs"] == 0
    inv = it.load_inventory()
    assert inv[KEY]["quantity"] == 40.0 and inv[KEY]["on_order"][0]["qty"] == 56


def test_absorbed_po_comes_back_on_order(env):
    it, sync, c = env
    inv = it.load_inventory()
    inv[KEY].update(quantity=40.0, last_count_at=f"{_day(-1)}T12:00:00")
    it.save_inventory(inv)
    _book(sync, "7600004O", order_day=_day(-40))
    it.load_inventory()                             # the read absorbs it
    assert any(e.get("source") == "on_order_absorbed" for e in it.load_usage())
    r = c.post("/api/pending/set-status", json={"po_number": "7600004O",
                                                "status": "in_production"},
               headers=H).get_json()
    assert r["moved_back_on_order"]["cs"] == 56
    inv = it.load_inventory()
    assert inv[KEY]["quantity"] == 40.0 and inv[KEY]["on_order"]
    assert _ledger("7600004O")["status"] == "in_production"


# --- the arrived-PO editor ---------------------------------------------------

def test_future_arrival_on_an_arrived_po_moves_it_back_on_order(env):
    it, sync, c = env
    q_rolled = _rolled(it, sync, po="7600005O")
    r = c.post("/api/pos/arrived/adjust", json={"po_number": "7600005O",
                                                "arrival_date": _day(10)},
               headers=H).get_json()
    assert r["ok"] and r["unrolled"] and r["onhand_delta_cs"] == -72
    inv = it.load_inventory()
    e = inv[KEY]["on_order"][0]
    assert e["arrival_date"].startswith(_day(10)) and e["arrival_source"] == "operator"
    assert inv[KEY]["quantity"] == q_rolled - 72
    assert _ledger("7600005O")["status"] == "pending"


def test_future_ship_date_on_an_arrived_po_estimates_a_future_arrival(env):
    it, sync, c = env
    _rolled(it, sync, po="7600006O")
    r = c.post("/api/pos/arrived/adjust", json={"po_number": "7600006O",
                                                "ship_date": _day(3)},
               headers=H).get_json()
    assert r["unrolled"]
    e = it.load_inventory()[KEY]["on_order"][0]
    assert e["ship_date"].startswith(_day(3)) and e["ship_date_source"] == "operator"
    assert e["arrival_date"] > _day(3) and e["arrival_source"] == "ship+transit"


def test_future_date_with_received_quantities_is_refused(env):
    it, sync, c = env
    _rolled(it, sync, po="7600007O")
    r = c.post("/api/pos/arrived/adjust", json={
        "po_number": "7600007O", "arrival_date": _day(5),
        "lines": [{"item_key": KEY, "qty": 50}]}, headers=H)
    assert r.status_code == 400
    assert not it.load_inventory()[KEY]["on_order"]


def test_past_arrival_edit_is_still_an_in_place_adjust(env):
    it, sync, c = env
    _rolled(it, sync, po="7600008O")
    r = c.post("/api/pos/arrived/adjust", json={"po_number": "7600008O",
                                                "arrival_date": _day(-1)},
               headers=H).get_json()
    assert r["ok"] and not r.get("unrolled")
    assert not it.load_inventory()[KEY]["on_order"]


# --- Arrived with a date -----------------------------------------------------

def test_arrived_with_a_date_rolls_the_po_in(env):
    it, sync, c = env
    _book(sync, "7600009O", order_day=_day(-3))
    q0 = it.load_inventory()[KEY]["quantity"]
    r = c.post("/api/pending/set-status", json={"po_number": "7600009O",
                                                "status": "arrived",
                                                "arrival_date": _day(0)},
               headers=H).get_json()
    assert r["arrival_stamped_lines"] == 1
    inv = it.load_inventory()
    assert not inv[KEY]["on_order"] and inv[KEY]["quantity"] == q0 + 56
    assert _ledger("7600009O")["status"] == "arrived"


def test_arrived_without_a_date_stays_a_label(env):
    it, sync, c = env
    _book(sync, "7600010O", order_day=_day(-3))
    c.post("/api/pending/set-status", json={"po_number": "7600010O",
                                            "status": "arrived"}, headers=H)
    assert it.load_inventory()[KEY]["on_order"]


def test_reopen_still_returns_the_po_as_open_without_dates(env):
    it, sync, c = env
    _rolled(it, sync, po="7600011O")
    r = c.post("/api/pending/reopen", json={"po_number": "7600011O"},
               headers=H).get_json()
    assert r["ok"] and r["removed_cs"] == 72
    e = it.load_inventory()[KEY]["on_order"][0]
    assert e["eta"] == e["ship_date"] == e["arrival_date"] == ""
