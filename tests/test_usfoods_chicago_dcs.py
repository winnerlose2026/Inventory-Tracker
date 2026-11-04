"""Both US Foods Chicago-metro DCs must resolve to the "Chicago, IL" warehouse.

Neither ever ingested a PO. On every USF PO the SHIPPING POINT label is the
metro name ("US FOODS-CHICAGO", "US FOODS-AURORA") but the SHIP TO city is the
actual town -- BENSENVILLE and AURORA. USF_DC_CITY_TO_WAREHOUSE only had
"CHICAGO", which matches no ship-to city, so `_usfoods_po_to_events` dropped
every line of every Chicago PO exactly the way it dropped Houston's.

Confirmed against the real PDFs:
    3T/2088  US FOODS-AURORA   2810 Duke Pkwy,  AURORA IL 60502      (PO 275346)
    3Y/2099  US FOODS-CHICAGO  800 Supreme Dr,  BENSENVILLE IL 60106 (PO 843051)
"""
from integrations.usfoods_po_parser import (
    USF_DC_CITY_TO_WAREHOUSE, parse_po_text,
)

# PO 275346 3T/2088, received 2026-07-24. 168 cs across 3 items.
AURORA_PO_TEXT = """\
 PURCHASE ORDER NO.    2753463T 0000001    *        US  F O O D S                *           07/24/2026 12:50:21
                               ORDER DATE: 07/24/26  CANCEL DATE: 00/00/00     PREPAID  X   COLLECT
                               SCHEDULE SHIPMENT TO ARRIVE ON: 08/14/26
  VENDOR 150345
 --------- V E N D O R --------        -------- B I L L   T O -------          S H I P P I N G   P O I N T
 H&H BAGELS                            US FOODS-AURORA
 -------- M A I L   T O -------        -------- S H I P   T O -------
 H&H BAGELS                            2088 AURORA/3T
 54-18 37TH AVE                        2810 DUKE PARKWAY
 QUEENS          NY 11377              AURORA          IL 60502         ---------- R E M A R K S ----------
 BUYER: 966 G X GARCIA                                                  APPROVED
ORDER ORDER   SCC/GTIN CODE      PACK SIZE - LABEL     ITEM
              10859313006912  6/10/4.06OZ -H&HBAGELS  1055064
   56 CASES   1159            BAGEL, ASIGO CHS WHEAT 4.06 Z        27.00                          27.00
              10859313006240  6/10/4.06OZ -H&HBAGELS  1055074
   56 CASES   1189            BAGEL, JLP CHEDR CHS WHEAT Z &       27.00                          27.00
              00859313006502  10/6/4.25OZ -HHMDTWBGES 7095637
   56 CASES   1150            BAGEL, PLN 4.25 Z UNSL PARBK         27.00                          27.00
 **** TOTALS **** 168   UNITS  2861 WEIGHT  217 CUBE
"""

# PO 843051 3Y/2099, received 2026-02-05. Legacy Bensenville DC.
BENSENVILLE_PO_TEXT = """\
 PURCHASE ORDER NO.    8430513Y 0000003    *        US  F O O D S                *           02/05/2026 07:41:24
                               ORDER DATE: 02/05/26  CANCEL DATE: 00/00/00     PREPAID  X   COLLECT
                               SCHEDULE SHIPMENT TO ARRIVE ON: 02/26/26
  VENDOR 150345
 --------- V E N D O R --------        -------- B I L L   T O -------          S H I P P I N G   P O I N T
 H&H BAGELS                            US FOODS-CHICAGO
 -------- M A I L   T O -------        -------- S H I P   T O -------
 H&H BAGELS                            2099 CHICAGO/3Y
 54-18 37TH AVE                        800 SUPREME DRIVE
 QUEENS          NY 11377              BENSENVILLE     IL 60106         ---------- R E M A R K S ----------
 BUYER: 751 E X SEVERING
ORDER ORDER   SCC/GTIN CODE      PACK SIZE - LABEL     ITEM
              10859313006226  6/10/4.06OZ -H&HBAGELS  1055010
   19 CASES   1184            BAGEL, EGG 4.06 Z UNSL HEAT &        27.00                          27.00
              00859313006588  10/6/4.25OZ -HHMDTWBGES 7309056
   82 CASES   1158            BAGEL, EVTHG 4 Z UNSL PARBK          27.00                          27.00
 **** TOTALS **** 391   UNITS  6782 WEIGHT  494 CUBE
"""


def test_both_chicago_dcs_are_mapped():
    assert USF_DC_CITY_TO_WAREHOUSE["AURORA"] == "Chicago, IL"
    assert USF_DC_CITY_TO_WAREHOUSE["BENSENVILLE"] == "Chicago, IL"


def test_aurora_po_resolves_to_chicago():
    po = parse_po_text(AURORA_PO_TEXT)
    assert po.po_number == "2753463T"
    assert po.ship_to_city == "AURORA"
    assert po.ship_to_state == "IL"
    assert po.ship_to_zip == "60502"
    # Was None -> all 3 lines dropped.
    assert po.warehouse == "Chicago, IL"
    assert po.buyer == "G X GARCIA"
    assert po.unmapped_items == []
    assert sum(l.quantity for l in po.lines) == 168
    assert [l.variety for l in po.lines] == [
        "Asiago", "Jalapeno Cheddar", "Plain",
    ]


def test_bensenville_po_resolves_to_chicago():
    """Legacy DC. Being replaced by Aurora, but must still resolve so its
    historical POs ingest on a backfill scan."""
    po = parse_po_text(BENSENVILLE_PO_TEXT)
    assert po.ship_to_city == "BENSENVILLE"
    assert po.warehouse == "Chicago, IL"
    assert po.buyer == "E X SEVERING"
    assert [(l.variety, l.quantity) for l in po.lines] == [
        ("Egg", 19.0), ("Everything", 82.0),
    ]


def test_shipping_point_label_is_not_used_as_the_city():
    """The Bensenville PO's SHIPPING POINT reads 'US FOODS-CHICAGO' while its
    ship-to address is Bensenville. Assert we read the address block, because
    trusting the metro label is what produced the original bad mapping."""
    po = parse_po_text(BENSENVILLE_PO_TEXT)
    assert po.ship_to_city == "BENSENVILLE"   # not "CHICAGO"
    po2 = parse_po_text(AURORA_PO_TEXT)
    assert po2.ship_to_city == "AURORA"


def test_chicago_pos_produce_restock_events():
    """No line may be dropped by _usfoods_po_to_events' guard:
        if not line.variety or not po.warehouse: continue
    """
    import integrations.usfoods_po_parser as parser

    for text, expect_cs in ((AURORA_PO_TEXT, 168), (BENSENVILLE_PO_TEXT, 101)):
        po = parser.parse_po_text(text)
        kept = [l for l in po.lines if l.variety and po.warehouse]
        assert len(kept) == len(po.lines), "a line would still be dropped"
        assert sum(l.quantity for l in kept) == expect_cs
        assert po.warehouse == "Chicago, IL"
