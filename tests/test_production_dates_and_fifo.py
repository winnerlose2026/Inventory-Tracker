#!/usr/bin/env python3
"""A sheet's production date is the most common PLAUSIBLE lot date, and FIFO
orders lots by their own encoded date.

Before: the date was the first typed date, else the EARLIEST lot date. One
mistyped lot (1189031026 for 091026) filed Riviera 014511751616 -- baked
9/10, emailed 9/11 -- under 2026-03-10, so FIFO drained its 168 cs of Plain
first and showed a July lot as "Next out". La Mirada 5579094C (Sept) was
filed under 2026-03-23 the same way. Lots on a canceled PO also counted.
"""
import sys
sys.path.insert(0, ".")

import inventory_tracker as it
from integrations.production_pdf_parser import (
    choose_production_date, is_plausible_production_date, lot_date_iso,
    parse_production_text, received_local_date,
)

RIVIERA_TEXT = (
    "Production Date\nCS Count SKU\n168 CS PARB-PLAIN\n40 CS PARB-ONION\n"
    "72 CS PARB-JALAPENO\n280 Total CasesRIVIERA BEACH.PO.014511751616\nLot#\n"
    "1150091026\n1151091026\n1189031026\n"
)


def test_one_typo_lot_no_longer_backdates_the_sheet():
    s = parse_production_text(RIVIERA_TEXT, subject="Daily Production RIVIERA BEACH PO.014511751616",
                              received_at="2026-09-11T14:02:00Z")
    assert s.production_date == "2026-09-10"


def test_without_received_at_the_old_rule_stands():
    s = parse_production_text(RIVIERA_TEXT)
    assert s.production_date == "2026-03-10"     # earliest lot, as before


def test_plausible_typed_date_used_when_no_plausible_lot():
    text = ("CS Count SKU\n56 CS PLAIN\n56 Total CasesCWNY.PO.1209205\n"
            "10/07/2026\n")
    s = parse_production_text(text, received_at="2026-10-08T15:42:51Z")
    assert s.production_date == "2026-10-07"


def test_implausible_everything_falls_back_to_received_date():
    # typed year 2016 (Ocala 054511644250) and no lots
    assert choose_production_date([], ["2016-01-19"], "2026-01-20T13:00:00Z") == "2026-01-20"


def test_most_common_lot_date_wins_ties_go_earliest():
    recv = "2026-10-16T19:53:02Z"
    assert choose_production_date(["2026-10-13"] * 3 + ["2026-10-16"], [], recv) == "2026-10-13"
    assert choose_production_date(["2026-10-14", "2026-10-15"], [], recv) == "2026-10-14"
    # a future-dated lot (121526 typo for 101526) is not plausible
    assert not is_plausible_production_date("2026-12-15", recv)


def test_received_date_is_the_bakery_local_date():
    # 01:30 UTC on the 11th is still the 10th in New York
    assert str(received_local_date("2026-09-11T01:30:00Z")) == "2026-09-10"
    assert lot_date_iso("1150091026") == "2026-09-10"
    assert lot_date_iso("115010012026") == "2026-10-01"
    assert lot_date_iso("1150133026") == ""          # month 13


def _rec(po, date, recv, lots_cs):
    return {"warehouse": "Riviera Beach, FL", "po_number": po, "production_date": date,
            "received_at": recv,
            "lines": [{"variety": "Plain", "cs_count": cs, "lot_number": lot}
                      for lot, cs in lots_cs]}


def _inv(qty):
    return {"plain bagel 4oz [cb - riviera beach]": {
        "name": "Plain Bagel 4oz [CB - Riviera Beach]", "warehouse": "Riviera Beach, FL",
        "quantity": qty, "on_order": []}}


def test_fifo_sorts_by_the_lots_own_date():
    recs = [
        _rec("014511715932", "2026-07-06", "2026-07-07T14:00:00Z", [("1150070626", 200)]),
        # stored with the back-dated sheet date, but its lot says 9/10
        _rec("014511751616", "2026-03-10", "2026-09-11T14:00:00Z", [("1150091026", 168)]),
    ]
    lots = it.compute_lot_fifo_state("Riviera Beach, FL", "Plain", production_records=recs,
                                     usage_records=[], inventory_snapshot=_inv(117.9),
                                     canceled_pos=set())
    assert [L["lot"] for L in lots] == ["1150070626", "1150091026"]
    nxt = [L for L in lots if L["is_next_out"]][0]
    assert nxt["lot"] == "1150091026" and round(nxt["cs_remaining"], 1) == 117.9
    assert lots[0]["cs_remaining"] == 0


def test_fifo_skips_lots_on_canceled_pos():
    recs = [
        _rec("014511715932", "2026-07-06", "2026-07-07T14:00:00Z", [("1150070626", 200)]),
        _rec("014599999999", "2026-07-08", "2026-07-09T14:00:00Z", [("1150070826", 56)]),
    ]
    lots = it.compute_lot_fifo_state("Riviera Beach, FL", "Plain", production_records=recs,
                                     usage_records=[], inventory_snapshot=_inv(100),
                                     canceled_pos={"014599999999"})
    assert [L["po_number"] for L in lots] == ["014511715932"]


def test_fifo_reads_canceled_pos_when_not_given(monkeypatch):
    monkeypatch.setattr(it, "load_canceled_pos", lambda: {"014599999999": {}})
    monkeypatch.setattr(it, "load_status_overrides", lambda: {})
    monkeypatch.setattr(it, "load_chefs_warehouse_pos", lambda: [])
    recs = [_rec("014599999999", "2026-07-08", "2026-07-09T14:00:00Z", [("1150070826", 56)])]
    lots = it.compute_lot_fifo_state("Riviera Beach, FL", "Plain", production_records=recs,
                                     usage_records=[], inventory_snapshot=_inv(10))
    assert lots == []
