#!/usr/bin/env python3
"""Tests for the canonical PO ledger builder (Phase 2). pytest or standalone."""
import sys
sys.path.insert(0, ".")
import inventory_tracker as it
import blueprints.pos as pos


def _setup(monkey):
    # pending USF (Zebulon) + pending Cheney (Punta Gorda, transfer pool)
    inv = {
        "plain bagel 4oz [usf - zebulon]": {
            "name": "Plain Bagel 4oz [USF - Zebulon]", "distributor": "US Foods",
            "warehouse": "Zebulon, NC",
            "on_order": [{"po_number": "PEND-USF", "qty": 32, "ordered_at": "2026-06-20",
                          "eta": "2026-07-20"}]},
        "everything bagel 4oz [cb - punta gorda]": {
            "name": "Everything Bagel 4oz [CB - Punta Gorda]", "distributor": "Cheney Brothers",
            "warehouse": "Punta Gorda, FL",
            "on_order": [{"po_number": "PEND-CB", "qty": 56, "ordered_at": "2026-06-22",
                          "ship_date": "2026-06-26T00:00:00", "arrival_date": "2026-07-03T00:00:00"}]},
        "sesame bagel 4oz [usf - alcoa]": {
            "name": "Sesame Bagel 4oz [USF - Alcoa]", "distributor": "US Foods",
            "warehouse": "Alcoa, TN", "on_order": []},
    }
    usage = [  # arrived USF PO via rollover
        {"source": "on_order_rollover", "po_number": "ARR-USF", "item_key": "sesame bagel 4oz [usf - alcoa]",
         "amount": -112, "unit": "cs", "ordered_at": "2026-05-01", "timestamp": "2026-05-31T10:00:00"},
        {"source": "on_order_rollover", "po_number": "ARR-USF", "item_key": "sesame bagel 4oz [usf - alcoa]",
         "amount": -56, "unit": "cs", "timestamp": "2026-05-31T10:00:00"},
        {"source": "email", "po_number": "IGNORED", "amount": -5},   # not a rollover -> ignored
    ]
    cw = [
        {"po_number": "CW-ARR", "warehouse": "Bronx, NY", "total_cs": 168,
         "lines": [{"variety": "Plain", "qty": 168}], "arrival_date": "2026-06-01T00:00:00"},  # past -> arrived
        {"po_number": "CW-PEND", "warehouse": "Chicago, IL", "total_cs": 112,
         "lines": [{"variety": "Everything", "qty": 112}]},  # no arrival -> pending
        {"po_number": "CW-CANX", "warehouse": "Opa Locka, FL", "total_cs": 56,
         "lines": [{"variety": "Sesame", "qty": 56}], "canceled": True},
    ]
    monkey(it, "load_inventory", lambda: inv)
    monkey(it, "load_usage", lambda: usage)
    monkey(it, "load_chefs_warehouse_pos", lambda: cw)
    monkey(it, "load_canceled_pos", lambda: {})
    monkey(it, "load_status_overrides", lambda: {})
    monkey(pos, "_freight_ship_date_index", lambda: {})


def _run():
    saved = {}
    def monkey(mod, name, fn):
        saved[(mod, name)] = getattr(mod, name)
        setattr(mod, name, fn)
    try:
        _setup(monkey)
        led = {r["po_number"]: r for r in pos.build_po_ledger()}
        assert set(led) == {"PEND-USF", "PEND-CB", "ARR-USF", "CW-ARR", "CW-PEND", "CW-CANX"}, set(led)
        assert led["PEND-USF"]["status"] == "pending" and led["PEND-USF"]["sources"] == ["on_order"]
        assert led["PEND-CB"]["status"] == "in_transit"          # ship_date set, arrival future
        assert led["PEND-CB"]["transfer_group"] == "cheney-fl"    # Cheney FL pool
        assert led["PEND-USF"]["transfer_group"] is None
        assert led["ARR-USF"]["status"] == "arrived" and led["ARR-USF"]["total_cs"] == 168.0
        assert led["ARR-USF"]["sources"] == ["usage_rollover"]
        assert led["CW-ARR"]["status"] == "arrived"              # arrival in past
        assert led["CW-PEND"]["status"] == "pending"
        assert led["CW-CANX"]["status"] == "canceled"
        # Phase 2b enrichment for the frontend read-flip
        assert led["CW-ARR"]["source_kind"] == "chefs_warehouse"
        assert led["ARR-USF"]["source_kind"] == "arrived"
        assert led["PEND-USF"]["source_kind"] == "inventory"
        assert "override" in led["PEND-USF"] and "dc_code" in led["CW-ARR"]
        assert "IGNORED" not in led                               # non-rollover usage ignored
        print("ok: all PO-ledger merge assertions passed")
        return 0
    finally:
        for (mod, name), fn in saved.items():
            setattr(mod, name, fn)


def test_po_ledger_merge():
    assert _run() == 0


if __name__ == "__main__":
    sys.exit(_run())
