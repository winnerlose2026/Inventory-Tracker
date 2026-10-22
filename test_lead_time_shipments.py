"""A truck is one delivery, however many POs ride on it.

Bronx (CW) read as "learned" at 3 days on 2026-10-22 off ONE truck: 1198387
and 1209205 left on the same 10/09 load, were confirmed in the same Lineage
message ("...-shows delivered 1225/12th") and delivered 10/12 12:25 -- two
POs, one sample. MIN_SAMPLES exists so one odd load doesn't move every
estimate; counting POs defeated it.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import inventory_tracker as it
from integrations import lead_times as LT
from integrations import po_delivery as D

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
BELINDA = "bmorris@onelineage.com"
META = {"1198387": {"distributor": "Chefs Warehouse", "warehouse": "Bronx, NY",
                    "ship_date": "", "ordered_at": "2026-09-30T00:00:00"},
        "1209205": {"distributor": "Chefs Warehouse", "warehouse": "Bronx, NY",
                    "ship_date": "", "ordered_at": "2026-10-01T00:00:00"}}
FREIGHT = {"1198387": "2026-10-09", "1209205": "2026-10-09"}   # invoices 7020718918/919


def _delivered(po, at, mid):
    rec, _, err = D.apply_event(None, {
        "po_number": po, "event": "delivered", "delivered_at": at,
        "evidence": {"message_id": mid, "sender": BELINDA, "sent": "2026-10-18T22:34:34Z",
                     "quote": f"{po}-shows delivered {at[11:13]}{at[14:16]}/{int(at[8:10])}th"}},
        NOW)
    assert err is None, err
    return rec


def _model(tracking):
    s, _ = LT.collect_samples(tracking, META, FREIGHT, D.norm_po, D.wall_time)
    return s, LT.build_model(s, lambda _wh: 7)


def test_two_pos_on_one_truck_are_one_delivery():
    trk = {"1198387": _delivered("1198387", "2026-10-12T12:25:00-04:00", "<m1>"),
           "1209205": _delivered("1209205", "2026-10-12T12:25:00-04:00", "<m1>")}
    samples, m = _model(trk)
    bronx = m["Chefs Warehouse|Bronx, NY"]
    assert bronx["transit_n"] == 1 and bronx["po_n"] == 2
    assert bronx["learned"] is False and bronx["used_days"] == 7
    assert "1 delivery so far" in bronx["basis"]
    assert bronx["shipments"] == [{"pos": ["1198387", "1209205"], "ship_date": "2026-10-09",
                                   "delivered_at": "2026-10-12T12:25:00", "transit_days": 3}]
    # the deliveries list still shows each PO, with its truck
    assert {s["po_number"] for s in samples} == {"1198387", "1209205"}
    assert samples[0]["shipment"] == samples[1]["shipment"]
    assert samples[0]["shipment_pos"] == ["1198387", "1209205"]


def test_same_ship_and_delivery_time_without_a_message_id_is_one_truck():
    trk = {"1198387": _delivered("1198387", "2026-10-12T12:25:00-04:00", "<a>"),
           "1209205": _delivered("1209205", "2026-10-12T12:25:00-04:00", "<b>")}
    assert _model(trk)[1]["Chefs Warehouse|Bronx, NY"]["transit_n"] == 1


def test_separate_trucks_still_count_separately():
    trk = {"1198387": _delivered("1198387", "2026-10-12T12:25:00-04:00", "<a>"),
           "1209205": _delivered("1209205", "2026-10-13T09:00:00-04:00", "<b>")}
    bronx = _model(trk)[1]["Chefs Warehouse|Bronx, NY"]
    assert bronx["transit_n"] == 2 and bronx["learned"] is True and bronx["used_days"] == 4


def test_rederive_puts_bronx_back_on_the_default(monkeypatch):
    import blueprints.freight as F
    trk = {"1198387": _delivered("1198387", "2026-10-12T12:25:00-04:00", "<m1>"),
           "1209205": _delivered("1209205", "2026-10-12T12:25:00-04:00", "<m1>")}
    _, model = _model(trk)
    ship = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT00:00:00")
    cw = [{"po_number": "1220439", "warehouse": "Bronx, NY", "ship_date": ship,
           "arrival_source": "freight", "transit_days": 3,
           "arrival_date": (datetime.fromisoformat(ship) + timedelta(days=3)).isoformat()}]
    store = {"inv": {}, "cw": cw}
    monkeypatch.setattr(it, "load_inventory", lambda: store["inv"])
    monkeypatch.setattr(it, "save_inventory", lambda d: store.update(inv=d))
    monkeypatch.setattr(it, "load_chefs_warehouse_pos", lambda: store["cw"])
    monkeypatch.setattr(it, "save_chefs_warehouse_pos", lambda d: store.update(cw=d))
    monkeypatch.setattr(F, "_freight_ship_date_index", lambda: {})
    rep = F.rederive_estimated_arrivals(dry_run=False, model=model)
    assert rep["pos_changed"] == 1 and rep["changes"][0]["transit_days"] == 7
    assert store["cw"][0]["arrival_date"][:10] == (
        datetime.fromisoformat(ship) + timedelta(days=7)).date().isoformat()


def test_lead_times_cache_follows_planning_config_and_cancels(monkeypatch, tmp_path):
    import blueprints.freight as F
    import blueprints.pos as P
    from app import app
    from core.cache import _AGG_CACHE
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    cfg = tmp_path / "planning_config.json"
    cfg.write_text("{}")
    monkeypatch.setenv("PLANNING_CONFIG_FILE", str(cfg))
    calls = []
    monkeypatch.setattr(F, "current_lead_time_model",
                        lambda: calls.append(1) or {"model": {}, "samples": [], "excluded": []})
    monkeypatch.setattr(P, "build_po_ledger", lambda: [])
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    _AGG_CACHE.pop("lead-times", None)
    c = app.test_client()
    h = {"X-Inventory-Token": "tok"}

    def hit():
        assert c.get("/api/freight/lead-times", headers=h).get_json()["ok"]
    hit(); hit()
    assert len(calls) == 1                        # cached
    for name in ("planning_config.json", "canceled_pos.json", "status_overrides.json"):
        p = tmp_path / name
        p.write_text('{"x": 1}')
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
        n = len(calls)
        hit()
        assert len(calls) == n + 1, name           # an edit there invalidates it
    _AGG_CACHE.pop("lead-times", None)


def test_lead_times_apply_writes_an_audit_row(monkeypatch):
    import blueprints.freight as F
    from app import app
    rows = []
    monkeypatch.setattr(F, "rederive_estimated_arrivals", lambda dry_run: {
        "dry_run": dry_run, "pos_changed": 1, "rows_changed": 1, "changes": [{
            "po_number": "1220439", "where": "cw", "warehouse": "Bronx, NY",
            "ship_date": "2026-10-24", "old_arrival": "2026-10-27",
            "new_arrival": "2026-10-31", "transit_days": 7, "basis": "7-day default"}]})
    monkeypatch.setattr(it, "append_inventory_audit", lambda r: rows.extend(r))
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    c = app.test_client()
    h = {"X-Inventory-Token": "tok"}
    assert c.post("/api/freight/lead-times/apply", json={}, headers=h).get_json()["dry_run"]
    assert rows == []                               # dry run by default
    out = c.post("/api/freight/lead-times/apply", json={"dry_run": False}, headers=h).get_json()
    assert out["ok"] and rows and rows[0]["source"] == "lead-times-apply"
    assert (rows[0]["old_arrival"], rows[0]["new_arrival"]) == ("2026-10-27", "2026-10-31")
