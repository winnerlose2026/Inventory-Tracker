"""Regression tests for the NW Houston (DC B2) US Foods DC.

PO 305202B2 arrived 2026-07-30 and every one of its 12 lines was silently
dropped: the PDF parsed fine, but "HOUSTON" was missing from
USF_DC_CITY_TO_WAREHOUSE, so `_usfoods_po_to_events` skipped each line
(`if not line.variety or not po.warehouse: continue`) and emitted an error
instead. The cron still exited 0, so the gap was invisible for two days.
"""
from integrations.usfoods_po_parser import (
    USF_DC_CITY_TO_WAREHOUSE, parse_po_text,
)

# Trimmed to the parts the parser reads: header, the two-column ship-to
# block, and two item pairs. Spacing is copied from the real PDF text
# extraction -- the ship-to regex depends on the 2+ space gutters.
HOUSTON_PO_TEXT = """\
 PURCHASE ORDER NO.    305202B2 0000003    *        US  F O O D S                *           07/30/2026 13:46:49
                               ORDER DATE: 07/29/26  CANCEL DATE: 00/00/00     PREPAID  X   COLLECT
                               SCHEDULE SHIPMENT TO ARRIVE ON: 08/31/26
  VENDOR 150345
 -------- M A I L   T O -------        -------- S H I P   T O -------
 H&H BAGELS                            3350 NW HOUSTON/B2
 54-18 37TH AVE                        13400 HOLLISTER ROAD
 QUEENS          NY 11377              HOUSTON         TX 77086         ---------- R E M A R K S ----------
 BUYER: 152 T X FOLEY                                                   INFO@HHBAGELS.COM
ORDER ORDER   SCC/GTIN CODE      PACK SIZE - LABEL     ITEM
              00859313006502  10/6/4.25OZ -HHMDTWBGES 7095637
  168 CASES   1150            BAGEL, PLN 4.25 Z UNSL PARBK         27.00                          27.00
              10859313006226  6/10/4.06OZ -H&HBAGELS  1055010
   56 CASES   1184            BAGEL, EGG 4.06 Z UNSL HEAT &        27.00                          27.00
 **** TOTALS **** 1120   UNITS  19440 WEIGHT  1423 CUBE
"""


def test_houston_dc_is_mapped():
    assert USF_DC_CITY_TO_WAREHOUSE["HOUSTON"] == "Houston, TX"


def test_houston_po_resolves_warehouse_and_lines():
    po = parse_po_text(HOUSTON_PO_TEXT)
    assert po.po_number == "305202B2"
    assert po.po_revision == "0000003"
    assert po.ship_to_city == "HOUSTON"
    assert po.ship_to_state == "TX"
    # The bug: this was None, which dropped every line downstream.
    assert po.warehouse == "Houston, TX"
    assert po.arrive_date == "08/31/26"
    assert po.unmapped_items == []
    assert [(l.usf_item_no, l.variety, l.quantity, l.case_size) for l in po.lines] == [
        ("1150", "Plain", 168.0, 60),
        ("1184", "Egg", 56.0, 60),
    ]


def test_buyer_does_not_swallow_the_remarks_column():
    """The Houston layout prints REMARKS on the BUYER line; the old greedy
    [A-Z \\.]+ captured 'T X FOLEY ... INFO' as the buyer name."""
    po = parse_po_text(HOUSTON_PO_TEXT)
    assert po.buyer == "T X FOLEY"


def test_every_seeded_usf_warehouse_has_a_dc_city():
    """seed_bagels and the PO parser must agree on the DC list.

    Seeded but unmapped -> every PO line for that DC is parsed and then
    dropped (the Houston bug). Mapped but unseeded -> restock events are
    emitted for a warehouse with no matching SKU. Either way the order
    disappears quietly, so assert the two sets are identical.
    """
    from seed_bagels import WAREHOUSES
    seeded = {w for w, _short, _mult in WAREHOUSES["US Foods"]}
    mapped = set(USF_DC_CITY_TO_WAREHOUSE.values())
    assert seeded == mapped, (
        f"seeded-only={seeded - mapped}, parser-only={mapped - seeded}"
    )
