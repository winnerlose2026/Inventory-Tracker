"""A Cheney PO line whose Mfg# is lost to a page break still resolves.

Cheney prints the H&H mfg code on the line UNDER each item. When the item is
the last one on a page, pypdf's text has the next page's facility header
where that Mfg# line should be, so the parser found no code and
`_cheney_po_to_events` dropped the line. Three POs in Sept 2026 each booked
8 cs short because of it:

    054511758943 (Ocala)        line 30  BAGEL ONION PARBAKED
    054511767347 (Ocala)        line 140 BAGEL EGG PARBAKED
    064511757163 (Punta Gorda)  line 30  BAGEL WHOLE WHEAT PARBAKED

Cheney's own catalog number is on the head line, so the parser now falls
back through CHENEY_ITEM_NO_TO_MFG, then the description.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from integrations.cheney_po_parser import parse_po_text  # noqa: E402

# Text as pypdf 3.17 extracts it from 054511758943 (2026-09-16 revision),
# trimmed to the first page's line items plus the page-2 header.
OCALA_PAGE_BREAK = """\
Purchase Order Number: 054511758943
Order Date: 09/09/2026
Vendor No: 119049
Vendor Name: H & H BAGELS
Buyer: STACY LYONS
Delivery Date 10/07/2026
Pickup/Delivery: DeliveryPage 1 of 4
Shipping Address OCALA FACILITY - CBI
2801 W SILVER SPRINGS BLVD
OCALA FL  34475
USA
We require an order acknowledgment for the following items:
Item Material/Description Brand Pack Size Quantity UM Unit Price         Net Amount
   10 10153018 BAGEL PLAIN PARBAKED H & H 001/60   CT    104 CS 26.50 CS 2756.00
                                          Mfg#   1150                                       GTIN#:
   20 10153019 BAGEL POPPY PARBAKED H & H 001/60   CT      8 CS 26.50 CS 212.00
                                          Mfg#   1152                                       GTIN#:
   30 10153034 BAGEL ONION PARBAKED H & H 001/60   CT      8 CS 26.50 CS 212.00

Cheney Brothers Inc
OCALA FACILITY - CBI
2801 W SILVER SPRINGS BLVD
OCALA FL  34475

******************************* ******************************* ********** *******************************
*OCALA  FACILITY*OCALA  FACILITY*OCALA  FACILITY*OCALA   FACILITY*OCALA  FACILITY*
                                                                                                                 Purchase order
PO number/date 4511758943 / 09/09/2026Page 2 of 4
Item Material/Description Brand Pack Size Quantity UM Unit Price         Net Amount
   40 10153041 BAGEL SESAME PARBAKED H & H 001/60   CT     24 CS 26.50 CS 636.00
                                          Mfg#   1153                                       GTIN#:
"""


def test_line_at_page_break_resolves_through_cheney_item_number():
    po = parse_po_text(OCALA_PAGE_BREAK)
    assert po.po_number == "054511758943"
    by_pos = {l.position: l for l in po.lines}
    onion = by_pos["30"]
    assert onion.variety == "Onion"
    assert onion.mfg_code == "1151"          # back-filled from the crosswalk
    assert onion.quantity == 8.0
    assert po.unmapped_items == []
    # Neighbours are untouched.
    assert by_pos["10"].variety == "Plain" and by_pos["10"].mfg_code == "1150"
    assert by_pos["40"].variety == "Sesame"


def test_unknown_catalog_number_falls_back_to_description():
    text = OCALA_PAGE_BREAK.replace("30 10153034 BAGEL ONION", "30 99999999 BAGEL ONION")
    po = parse_po_text(text)
    onion = next(l for l in po.lines if l.position == "30")
    assert onion.variety == "Onion"
    assert onion.mfg_code == ""              # nothing to back-fill from


def test_description_order_prefers_the_longer_name():
    from integrations.cheney_po_parser import _variety_from_description
    assert _variety_from_description("BAGEL WHOLE WHEAT EVERYTHING PARBAKED") == "Whole Wheat Everything"
    assert _variety_from_description("BAGEL WHOLE WHEAT PARBAKED") == "Whole Wheat"
    assert _variety_from_description("BAGEL CHEDDAR JALAPENO  PARBAKED") == "Jalapeno Cheddar"
    assert _variety_from_description("BAGEL EGG PARBAKED") == "Egg"
    assert _variety_from_description("SOMETHING ELSE") == ""
