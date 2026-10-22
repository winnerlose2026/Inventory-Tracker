"""Learned per-location transit replaces the blanket ship + 7.

JD 2026-10-22: "the lead times can be dialed in now based on the email
confirmations from Belinda and the Lineage team ... build that data into the
Pending PO's tab to give a better indication from the ship date to the Arrival
column instead of giving everything a blanket 7 day transit time."
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import inventory_tracker as it
from integrations import lead_times as LT
from integrations import po_delivery as D

BELINDA = "bmorris@onelineage.com"
NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)


def _default(_wh):
    return 7


def _delivered(po, at, pickup=""):
    rec, _, _ = D.apply_event(None, {
        "po_number": po, "event": "delivered", "delivered_at": at,
        "evidence": {"message_id": f"<{po}>", "sender": BELINDA,
                     "sent": "2026-10-20T12:00:00Z", "quote": "shows delivered"}}, NOW)
    if pickup:
        rec["pickup_at"] = pickup
    return rec


META = {
    "393072B2": {"distributor": "US Foods", "warehouse": "Houston, TX",
                 "ship_date": "2026-10-16T13:00:00", "ordered_at": "2026-09-28T00:00:00"},
    "1209205": {"distributor": "Chefs Warehouse", "warehouse": "Bronx, NY",
                "ship_date": "", "ordered_at": "2026-10-01T00:00:00"},
    "1198387": {"distributor": "Chefs Warehouse", "warehouse": "Bronx, NY",
                "ship_date": "", "ordered_at": "2026-09-30T00:00:00"},
    "1209157": {"distributor": "Chefs Warehouse", "warehouse": "Chicago, IL",
                "ship_date": "", "ordered_at": "2026-10-01T00:00:00"},
}


def _samples(tracking, freight=None, meta=META):
    return LT.collect_samples(tracking, meta, freight or {}, D.norm_po, D.wall_time)


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------

def test_transit_is_ship_to_confirmed_delivery_in_local_dates():
    trk = {"393072B2": _delivered("393072B2", "2026-10-19T08:00:00-05:00",
                                  pickup="2026-10-16T13:00:00-04:00")}
    s, ex = _samples(trk)
    assert not ex and len(s) == 1
    assert s[0]["transit_days"] == 3          # Fri pickup -> Mon 8:00 AM CT
    assert s[0]["ship_source"] == "Lineage pickup"
    assert s[0]["location"] == "Houston, TX"
    assert s[0]["order_to_delivery_days"] == 21


def test_freight_invoice_ship_date_wins():
    trk = {"1209205": _delivered("1209205", "2026-10-12T12:25:00-04:00",
                                 pickup="2026-10-10T09:00:00-04:00")}
    s, _ = _samples(trk, freight={"1209205": "2026-10-09"})
    assert s[0]["ship_date"] == "2026-10-09" and s[0]["ship_source"] == "freight invoice"
    assert s[0]["transit_days"] == 3
    assert s[0]["location"] == "Bronx, NY (CW)"


def test_unusable_deliveries_are_reported_not_used():
    trk = {"1209157": _delivered("1209157", "2026-10-20T08:00:00-05:00"),   # no ship date
           "ZZZ999": _delivered("ZZZ999", "2026-10-20T08:00:00-05:00"),     # not in ledger
           "1198387": _delivered("1198387", "2026-10-12T12:25:00-04:00")}
    s, ex = _samples(trk, freight={"1198387": "2026-08-01"})               # 72 d: bad pairing
    assert s == []
    reasons = {e["po_number"]: e["reason"] for e in ex}
    assert reasons["1209157"] == "no ship date"
    assert reasons["ZZZ999"] == "PO not in the ledger"
    assert "outside" in reasons["1198387"]


def test_requested_or_scheduled_pos_are_not_samples():
    rec, _, _ = D.apply_event(None, {"po_number": "393072B2", "event": "requested",
                                     "evidence": {"message_id": "<r>", "sent": "2026-10-12T19:16:29Z",
                                                  "quote": "PO #: 393072B2"}}, NOW)
    assert _samples({"393072B2": rec}) == ([], [])


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def _s(po, wh, ship, delivered, dist="US Foods"):
    return {"po_number": po, "distributor": dist, "warehouse": wh,
            "location": LT.location_label(dist, wh), "ship_date": ship,
            "ship_source": "freight invoice", "delivered_at": delivered,
            "transit_days": (datetime.fromisoformat(delivered[:10])
                             - datetime.fromisoformat(ship)).days,
            "order_to_delivery_days": None}


def test_one_delivery_is_shown_but_the_default_still_applies():
    m = LT.build_model([_s("A", "Houston, TX", "2026-10-16", "2026-10-19T08:00:00")], _default)
    h = m["US Foods|Houston, TX"]
    assert h["transit_n"] == 1 and h["trend_avg"] == 3
    assert h["learned"] is False and h["used_days"] == 7
    assert "need 2" in h["basis"]


def test_two_or_more_deliveries_replace_the_default_with_the_recent_trend():
    ss = [_s("A", "Zebulon, NC", "2026-06-05", "2026-06-09T08:00:00"),   # 4
          _s("B", "Zebulon, NC", "2026-07-24", "2026-07-28T08:00:00"),   # 4
          _s("C", "Zebulon, NC", "2026-08-21", "2026-08-25T08:00:00"),   # 4
          _s("D", "Zebulon, NC", "2026-09-04", "2026-09-14T08:00:00"),   # 10 (holiday)
          _s("E", "Zebulon, NC", "2026-09-25", "2026-09-29T08:00:00"),   # 4
          _s("F", "Zebulon, NC", "2026-10-09", "2026-10-12T08:00:00")]   # 3
    z = LT.build_model(ss, _default)["US Foods|Zebulon, NC"]
    assert z["learned"] is True and z["transit_n"] == 6
    assert z["trend_window"] == 5 and z["trend_avg"] == 5.0         # last 5: 4,4,10,4,3
    assert z["used_days"] == 5 and z["basis"] == "avg of last 5 deliveries"
    assert z["transit_median"] == 4.0 and z["transit_max"] == 10


def test_chicago_cw_and_chicago_usf_are_separate_lanes():
    m = LT.build_model([_s("A", "Chicago, IL", "2026-10-13", "2026-10-20T08:00:00", "Chefs Warehouse"),
                        _s("B", "Chicago, IL", "2026-09-30", "2026-10-03T08:00:00")], _default)
    assert set(m) == {"Chefs Warehouse|Chicago, IL", "US Foods|Chicago, IL"}


def test_estimate_uses_learned_days_or_default():
    m = LT.build_model([_s("A", "Bronx, NY", "2026-10-09", "2026-10-12T12:25:00", "Chefs Warehouse"),
                        _s("B", "Bronx, NY", "2026-10-02", "2026-10-06T09:00:00", "Chefs Warehouse")],
                       _default)
    arr, days, basis = LT.estimate_arrival("2026-10-23", "Chefs Warehouse", "Bronx, NY", m, _default)
    assert (arr, days) == ("2026-10-27T00:00:00", 4) and basis.startswith("avg of last 2")
    arr, days, basis = LT.estimate_arrival("2026-10-23", "US Foods", "Alcoa, TN", m, _default)
    assert (arr, days) == ("2026-10-30T00:00:00", 7) and "default" in basis


def test_which_arrivals_are_estimates():
    assert LT.arrival_is_derived({"arrival_source": "freight"})
    assert LT.arrival_is_derived({"arrival_source": "ship+transit"})
    assert not LT.arrival_is_derived({"arrival_source": "operator"})
    assert not LT.arrival_is_derived({"arrival_source": "lineage"})
    assert not LT.arrival_is_derived({"arrival_source": "lineage-delivered"})
    # legacy unlabeled: exactly ship + 7 was the old guess; anything else isn't
    assert LT.arrival_is_derived({"ship_date": "2026-10-16T00:00:00",
                                  "arrival_date": "2026-10-23T00:00:00"})
    assert not LT.arrival_is_derived({"ship_date": "2026-10-16T00:00:00",
                                      "arrival_date": "2026-10-21T00:00:00"})


# ---------------------------------------------------------------------------
# Re-deriving pending estimates
# ---------------------------------------------------------------------------

BRONX_MODEL = LT.build_model(
    [_s("A", "Bronx, NY", "2026-10-09", "2026-10-12T12:25:00", "Chefs Warehouse"),
     _s("B", "Bronx, NY", "2026-10-02", "2026-10-05T09:00:00", "Chefs Warehouse"),
     _s("C", "Manassas, VA", "2026-09-04", "2026-09-08T08:00:00"),
     _s("D", "Manassas, VA", "2026-10-13", "2026-10-15T08:00:00")], _default)


def _future(days):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")


def _rederive(monkeypatch, inv, cw):
    import blueprints.freight as F
    store = {"inv": inv, "cw": cw}
    monkeypatch.setattr(it, "load_inventory", lambda: store["inv"])
    monkeypatch.setattr(it, "save_inventory", lambda d: store.update(inv=d))
    monkeypatch.setattr(it, "load_chefs_warehouse_pos", lambda: store["cw"])
    monkeypatch.setattr(it, "save_chefs_warehouse_pos", lambda d: store.update(cw=d))
    monkeypatch.setattr(F, "_freight_ship_date_index", lambda: {})
    return F.rederive_estimated_arrivals(dry_run=False, model=BRONX_MODEL), store


def test_rederive_moves_estimates_only(monkeypatch):
    ship = _future(2)
    inv = {"plain bagel 4oz [usf - manassas]": {
        "name": "Plain Bagel 4oz [USF - Manassas]", "distributor": "US Foods",
        "warehouse": "Manassas, VA", "on_order": [
            {"po_number": "EST1", "ship_date": ship, "qty": 56,
             "arrival_date": (datetime.fromisoformat(ship) + timedelta(days=7)).isoformat()},
            {"po_number": "OPS1", "ship_date": ship, "qty": 56, "arrival_source": "operator",
             "arrival_date": _future(20)},
            {"po_number": "LIN1", "ship_date": ship, "qty": 56, "arrival_source": "lineage",
             "arrival_date": _future(21)}]}}
    cw = [{"po_number": "CWF", "warehouse": "Bronx, NY", "ship_date": ship,
           "arrival_date": (datetime.fromisoformat(ship) + timedelta(days=7)).isoformat()},
          {"po_number": "CWP", "warehouse": "Bronx, NY", "ship_date": "2026-09-01T00:00:00",
           "arrival_date": "2026-09-08T00:00:00"}]           # already past: history
    rep, store = _rederive(monkeypatch, inv, cw)
    rows = {e["po_number"]: e for e in store["inv"]["plain bagel 4oz [usf - manassas]"]["on_order"]}
    assert rows["EST1"]["arrival_date"][:10] == (datetime.fromisoformat(ship) + timedelta(days=3)).date().isoformat()
    assert rows["EST1"]["arrival_source"] == "ship+transit" and rows["EST1"]["transit_days"] == 3
    assert rows["OPS1"]["arrival_date"] == _future(20)
    assert rows["LIN1"]["arrival_date"] == _future(21)
    cwr = {r["po_number"]: r for r in store["cw"]}
    assert cwr["CWF"]["arrival_date"][:10] == (datetime.fromisoformat(ship) + timedelta(days=3)).date().isoformat()
    assert cwr["CWP"]["arrival_date"] == "2026-09-08T00:00:00"
    assert rep["pos_changed"] == 2
    # idempotent
    rep2, _ = _rederive(monkeypatch, store["inv"], store["cw"])
    assert rep2["pos_changed"] == 0


# ---------------------------------------------------------------------------
# Writers: the operator's ship date
# ---------------------------------------------------------------------------

def test_operator_ship_date_uses_the_locations_learned_transit(monkeypatch):
    import blueprints.freight as F
    from app import app
    inv = {"plain bagel 4oz [usf - manassas]": {
        "name": "Plain Bagel 4oz [USF - Manassas]", "distributor": "US Foods",
        "warehouse": "Manassas, VA", "on_order": [{"po_number": "P1", "qty": 56}]}}
    store = {"inv": inv}
    monkeypatch.setattr(it, "load_inventory", lambda: store["inv"])
    monkeypatch.setattr(it, "save_inventory", lambda d: store.update(inv=d))
    monkeypatch.setattr(F, "current_lead_time_model", lambda: {"model": BRONX_MODEL})
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    c = app.test_client()
    r = c.post("/api/on-order/ship-date", headers={"X-Inventory-Token": "tok"},
               json={"po_number": "P1", "ship_date": "2026-11-02"}).get_json()
    assert r["ok"] and r["arrival_date"] == "2026-11-05T00:00:00" and r["transit_days"] == 3
    e = store["inv"]["plain bagel 4oz [usf - manassas]"]["on_order"][0]
    assert e["arrival_source"] == "ship+transit" and e["eta"] == "2026-11-05T00:00:00"
    # an explicit arrival still wins
    r = c.post("/api/on-order/ship-date", headers={"X-Inventory-Token": "tok"},
               json={"po_number": "P1", "ship_date": "2026-11-02",
                     "arrival_date": "2026-11-09"}).get_json()
    assert r["arrival_date"] == "2026-11-09T00:00:00" and r["arrival_source"] == "operator"


def test_ledger_says_how_an_arrival_was_obtained(monkeypatch):
    import blueprints.pos as P
    inv = {"plain bagel 4oz [usf - manassas]": {
        "name": "Plain Bagel 4oz [USF - Manassas]", "distributor": "US Foods",
        "warehouse": "Manassas, VA", "on_order": [
            {"po_number": "P1", "qty": 56, "ship_date": _future(2), "arrival_date": _future(5),
             "arrival_source": "ship+transit", "transit_days": 3,
             "transit_basis": "avg of last 2 deliveries", "ordered_at": _future(-10)},
            {"po_number": "P2", "qty": 56, "ship_date": _future(2), "arrival_date": _future(9),
             "arrival_source": "operator", "ordered_at": _future(-10)}]}}
    monkeypatch.setattr(it, "load_inventory", lambda: inv)
    monkeypatch.setattr(it, "load_usage", lambda: [])
    monkeypatch.setattr(it, "load_chefs_warehouse_pos", lambda: [])
    monkeypatch.setattr(it, "load_canceled_pos", lambda: {})
    monkeypatch.setattr(it, "load_status_overrides", lambda: {})
    monkeypatch.setattr(it, "load_po_delivery", lambda: {})
    monkeypatch.setattr(P, "_freight_ship_date_index", lambda: {})
    led = {r["po_number"]: r for r in P.build_po_ledger()}
    assert led["P1"]["arrival_kind"] == "estimate" and led["P1"]["transit_days"] == 3
    assert led["P1"]["transit_basis"] == "avg of last 2 deliveries"
    assert led["P2"]["arrival_kind"] == "operator" and "transit_days" not in led["P2"]


# ---------------------------------------------------------------------------
# Evidence-only threads are never replied on
# ---------------------------------------------------------------------------

def test_evidence_subjects():
    from blueprints.delivery import _is_evidence_subject as ev
    assert ev("Re: H&H Bagels PO 3795383T Delivery Status")
    assert ev("RE: H&H Bagels PO Delivery - PO 014511743693")
    assert not ev("Re: Quote and Schedule - 6 Orders")
    assert not ev("Delivery update")


def test_followups_never_go_out_on_an_evidence_only_thread(monkeypatch):
    import blueprints.delivery as B
    from app import app
    rec, _, _ = D.apply_event(None, {"po_number": "P9", "event": "requested",
                                     "evidence": {"message_id": "<r>", "sent": "2026-10-10T12:00:00Z",
                                                  "quote": "PO #: P9"}}, NOW)
    cache = {"synced_at": (NOW - timedelta(minutes=5)).isoformat(), "errors": [],
             "messages": {}, "threads": {"ev1": {
                 "conversation_id": "ev1", "subject": "H&H Bagels PO Delivery Status",
                 "reply_to_graph_id": "", "last_sent": NOW.isoformat(),
                 "po_numbers": ["P9"], "evidence_only": True}}}
    monkeypatch.setattr(it, "load_po_delivery", lambda: {"P9": rec})
    monkeypatch.setattr(it, "save_po_delivery", lambda d: None)
    monkeypatch.setattr(it, "load_po_delivery_threads", lambda: cache)
    monkeypatch.setattr(B, "_ledger_by_po", lambda: {"P9": {"po_number": "P9", "status": "pending"}})
    monkeypatch.setattr(B, "_now", lambda: NOW)
    sent = []
    monkeypatch.setattr(B, "_send_reply", lambda *a, **k: sent.append(a) or "x")
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    r = app.test_client().post("/api/pos/delivery/send-followups",
                               headers={"X-Inventory-Token": "tok"}, json={}).get_json()
    assert r["ok"] and sent == [] and r["due_without_thread"] == ["P9"]
