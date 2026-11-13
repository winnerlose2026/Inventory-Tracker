"""One "has it shipped?" rule for the Planner and the Pending POs tab
(hardening follow-up 2026-11-13: a late PO read "In Transit" on Pending POs
and LATE on the Planner)."""
from datetime import date

from integrations.po_shipping import shipped_verdict

TODAY = date(2026, 11, 13)


def _po(**kw):
    base = {"status": "in_transit", "ship_date": "2026-11-10",
            "lines": [{"variety": "Plain", "qty": 336}], "production": None}
    base.update(kw)
    return base


def test_ship_date_gone_by_unbaked_is_late():
    v = shipped_verdict(_po(), TODAY)
    assert v == {"shipped": False, "late": True, "late_days": 4, "basis": "unverified"}


def test_freight_or_lineage_pickup_proves_it():
    assert shipped_verdict(_po(ship_date_source="freight"), TODAY)["shipped"]
    assert shipped_verdict(_po(ship_date_source="lineage"), TODAY)["shipped"]


def test_fully_baked_counts_as_shipped_partly_baked_does_not():
    assert shipped_verdict(_po(production={"baked_cs": 336}), TODAY)["basis"] == "baked"
    assert shipped_verdict(_po(production={"baked_cs": 112}), TODAY)["late"]


def test_operator_override_and_arrived_win():
    assert shipped_verdict(_po(override="in_transit"), TODAY)["basis"] == "operator"
    assert shipped_verdict(_po(status="arrived"), TODAY)["shipped"]


def test_future_ship_date_is_not_due_and_never_late():
    v = shipped_verdict(_po(status="pending", ship_date="2026-11-20"), TODAY)
    assert v["shipped"] is False and v["late"] is False and v["basis"] == "not_due"


def test_today_is_already_late_if_unproven():
    assert shipped_verdict(_po(ship_date="2026-11-13"), TODAY)["late_days"] == 1


def test_canceled_is_neither():
    v = shipped_verdict(_po(status="canceled"), TODAY)
    assert not v["shipped"] and not v["late"]


def test_planner_queue_agrees_with_the_ledger_verdict():
    from datetime import datetime
    from integrations.production_planner import build_production_guide
    from integrations.planning_config import load_planning_config
    late = _po(po_number="L1", warehouse="Ocala, FL", distributor="Cheney Brothers")
    shipped = _po(po_number="S1", warehouse="Ocala, FL",
                  distributor="Cheney Brothers", ship_date_source="freight")
    guide = build_production_guide(inv={}, ledger=[late, shipped], demand_rows=[],
                                   config=load_planning_config(),
                                   now=datetime(2026, 11, 13, 9))
    q = {r["po_number"]: r for r in guide.get("queue") or guide.get("production_queue") or []}
    assert "L1" in q and q["L1"]["late"] and "S1" not in q
