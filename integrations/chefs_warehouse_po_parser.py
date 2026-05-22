"""Parser for Chefs Warehouse PO PDF attachments.

Chefs Warehouse (CW) POs arrive from a buyer@chefswarehouse.com address
(specifically: nalopez@, mfiltidor@, etc., all on @chefswarehouse.com).
The PO is always a single text-extractable PDF attachment named
``PO <po#> <dc_code>.pdf`` where dc_code is a 2-3 letter DC tag
(NY, MD, FLA, CHI).

Layout (observed across 6 sample POs from Mar-May 2026):

    PURCHASE ORDER # 1068886
    *1068886*
    Supplier Warehouse:                67858343
    H&H MIDTOWN BAGELS EAST
    109 West 27th Street Suite 3N
    NEW YORK, NY, 10001
     ...
     Order Date     03-10-2026
    Pickup Date
    Delivery Date 03-31-2026
    Terms: Net 30 For A/P Freight Handling: VI Currency: USD ...
    Ship To: 400001 Buyer: 9480679
    The Chefs' Warehouse Mid-Atlantic, LLC NATALIE LOPEZ
    7477 Candlewood Rd. Receiving 5559 NW 145th ST Phone:
    Hanover, MD, 21076 OPA LOCKA, FL, 33054 Fax:
    ...
    Total USD 3,469.44
    3/10/2026 15:29:44 PM
    1  10507796SLD BAGELS HH PLAIN SLICED
    10/6 CT CS56.00 CS 31.6768 1,773.90
    21163  10507809 BAGELS HH SESAME
    10/6 CT CS20.00 CS 29.5000 590.00
    ...

Line-item blocks are TWO lines each:

    Line 1: <LN><optional vendor_item>  <CW item>[SLD] <description>
    Line 2: <pack> <pack_um> CS<qty> CS <unit_cost> <ext_cost>

The LN/vendor_item prefix is concatenated with no separator. LN is 1-9
in observed POs; vendor_item is 4-digit (e.g. 1160, 1163, 1165, 1168)
and present only on non-sliced lines. We disambiguate by tracking the
expected line number.

SLICED variants of a CW item carry the suffix ``SLD`` on the CW item
number (e.g. 10507796SLD = "PLAIN SLICED") and never carry a vendor
item code on the PO.

The ``Ship To`` header identifies the DC by CW's internal ID:

    200001 -> Dairyland USA Corporation, Bronx, NY      (NY DC)
    400001 -> The Chefs' Warehouse Mid-Atlantic,
              Hanover, MD                               (MD + CHI DCs;
                                                         CHI POs ship to
                                                         Hanover and are
                                                         transferred from
                                                         there)
    600001 -> The Chefs' Warehouse Florida,
              Opa Locka, FL                             (FLA DC)

The two lines under the Ship To header have the FACILITY city on the
left and the BUYER's office on the right; we pull the receiving city
from the first match of ``<City>, <ST>, <ZIP>`` inside that block.

CW does not expose a PO revision number on these layouts; po_revision
is left ``""`` so the apply path treats every fresh ingest of the same
po_number as idempotent (replace-by-po_number).

CW POs are NOT routed into ``data/inventory.json`` -- the user
explicitly keeps CW out of the Inventory tab. The scanner writes them
to ``data/chefs_warehouse_pos.json`` instead, and the Pending POs tab
merges them in for display only.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Optional

import pypdf


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

# Ship-to ID -> "<City>, <ST>" surfaced as ``warehouse``. CW Chicago
# DC ships into Hanover MD per the PO layout (subject says "CHI" but
# the PDF ships to Mid-Atlantic), so 400001 covers both MD and CHI.
CW_SHIP_TO_TO_WAREHOUSE: dict[str, str] = {
    "200001": "Bronx, NY",
    "400001": "Hanover, MD",
    "600001": "Opa Locka, FL",
}

# Fallback for unknown ship-to IDs -- match on the city extracted from
# the PDF address block.
CW_DC_CITY_TO_WAREHOUSE: dict[str, str] = {
    "BRONX":     "Bronx, NY",
    "HANOVER":   "Hanover, MD",
    "OPA LOCKA": "Opa Locka, FL",
}

# Description on CW POs -> canonical H&H variety. Slicing is tracked
# explicitly because CW carries both whole and sliced SKUs and the
# Daily Production sheet rolls them under the same variety with a
# "sliced" attribute. Extend as new flavors appear.
CW_DESCRIPTION_TO_VARIETY: dict[str, str] = {
    "BAGELS HH PLAIN":                  "Plain",
    "BAGELS HH PLAIN SLICED":           "Plain Sliced",
    "BAGELS HH SESAME":                 "Sesame",
    "BAGELS HH SESAME SLICED":          "Sesame Sliced",
    "BAGELS HH POPPY":                  "Poppy Seed",
    "BAGELS HH POPPY SLICED":           "Poppy Seed Sliced",
    "BAGELS HH POPPY SEED":             "Poppy Seed",
    "BAGELS HH POPPY SEED SLICED":      "Poppy Seed Sliced",
    "BAGELS HH CINNAMON RAISIN":        "Cinnamon Raisin",
    "BAGELS HH CINNAMON RAISIN SLICED": "Cinnamon Raisin Sliced",
    "BAGELS HH EVERYTHING":             "Everything",
    "BAGELS HH EVERYTHING SLICED":      "Everything Sliced",
    "BAGELS HH EGG":                    "Egg",
    "BAGELS HH BLUEBERRY":              "Blueberry",
    "BAGELS HH ASIAGO":                 "Asiago",
    "BAGELS HH JALAPENO CHEDDAR":       "Jalapeno Cheddar",
}


def _case_size_from_pack(pack: str) -> Optional[int]:
    """Pack '10/6 CT' = 10 sleeves of 6 = 60 bagels/case."""
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", pack or "")
    if not m:
        return None
    try:
        return int(m.group(1)) * int(m.group(2))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ChefsWarehousePOLine:
    line_no: int = 0
    vendor_item: str = ""           # H&H internal vendor item (4-digit) or ""
    cw_item: str = ""                # CW catalog item, may end with "SLD"
    description: str = ""            # e.g. "BAGELS HH PLAIN SLICED"
    variety: str = ""                # mapped via CW_DESCRIPTION_TO_VARIETY
    sliced: bool = False
    pack: str = ""                   # e.g. "10/6"
    pack_um: str = ""                # e.g. "CT"
    case_size: Optional[int] = None  # 60 for 10/6 CT
    quantity: float = 0.0            # cases
    quantity_um: str = "CS"
    unit_cost: Optional[float] = None
    ext_cost: Optional[float] = None


@dataclass
class ChefsWarehousePO:
    po_number: str = ""
    po_revision: str = ""           # CW POs don't expose revisions
    order_date: str = ""             # MM-DD-YYYY as printed
    delivery_date: str = ""          # MM-DD-YYYY as printed
    ship_to_id: str = ""             # e.g. "400001"
    ship_to_name: str = ""           # e.g. "The Chefs' Warehouse Mid-Atlantic, LLC"
    ship_to_city: str = ""           # e.g. "HANOVER"
    ship_to_state: str = ""          # e.g. "MD"
    ship_to_zip: str = ""
    warehouse: str = ""              # canonical "<City>, <ST>"
    dc_code: str = ""                # subject suffix hint (NY/MD/FLA/CHI), populated by caller
    buyer_id: str = ""
    buyer_name: str = ""             # e.g. "NATALIE LOPEZ"
    total_usd: Optional[float] = None
    lines: list = field(default_factory=list)
    unmapped_descriptions: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _extract_text(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def parse_po_pdf(pdf_bytes: bytes, dc_code: str = "") -> ChefsWarehousePO:
    return parse_po_text(_extract_text(pdf_bytes), dc_code=dc_code)


_PO_NUMBER_RE   = re.compile(r"PURCHASE ORDER #\s*(\d+)")
_ORDER_DATE_RE  = re.compile(r"Order Date\s+(\d{2}-\d{2}-\d{4})")
_DELIV_DATE_RE  = re.compile(r"Delivery Date\s+(\d{2}-\d{2}-\d{4})")
_SHIP_TO_RE     = re.compile(r"Ship To:\s*(\d+)\s+Buyer:\s*(\d+)")
_TOTAL_USD_RE   = re.compile(r"Total USD\s+([\d,]+\.\d{2})")

# Receiving address city/state -- first match of "<CITY>, <ST>, <ZIP>"
# inside the lines that follow the Ship To header.
_ADDR_CITY_RE   = re.compile(
    r"^([A-Z][A-Za-z .'\-]+?),\s+([A-Z]{2}),\s+(\d{5})"
)

# Line-item head: "<LN><optional vendor>  <cw_item> <description>".
# Two-or-more spaces separate the prefix from the cw_item.
_LINE_HEAD_RE   = re.compile(r"^(\d+)\s{2,}(\S+)\s+(.+?)\s*$")

# Detail line: "<pack> <pack_um> CS<qty> CS <unit_cost> <ext_cost>"
_LINE_DETAIL_RE = re.compile(
    r"^(?P<pack>\d+/\d+)\s+"
    r"(?P<pack_um>[A-Z]+)\s+"
    r"CS(?P<qty>[\d.,]+)\s+"
    r"(?P<qty_um>[A-Z]+)\s+"
    r"(?P<unit_cost>[\d.,]+)\s+"
    r"(?P<ext_cost>[\d.,]+)"
)


def parse_po_text(text: str, dc_code: str = "") -> ChefsWarehousePO:
    po = ChefsWarehousePO(dc_code=(dc_code or "").strip().upper())

    if m := _PO_NUMBER_RE.search(text):
        po.po_number = m.group(1)
    if m := _ORDER_DATE_RE.search(text):
        po.order_date = m.group(1)
    if m := _DELIV_DATE_RE.search(text):
        po.delivery_date = m.group(1)
    if m := _TOTAL_USD_RE.search(text):
        try:
            po.total_usd = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    if m := _SHIP_TO_RE.search(text):
        po.ship_to_id = m.group(1)
        po.buyer_id   = m.group(2)

    # Pull the receiving facility name + city/state from the block right
    # after the Ship To header.
    lines = text.splitlines()
    for idx, raw in enumerate(lines):
        if "Ship To:" not in raw or "Buyer:" not in raw:
            continue
        if idx + 1 < len(lines):
            facility_line = lines[idx + 1].strip()
            # Strip the trailing "BUYER NAME" (all-caps run at the end).
            mm = re.match(
                r"^(?P<name>.+?\b(?:LLC|Corp(?:oration)?|Inc))\s+"
                r"(?P<buyer>[A-Z][A-Z .'\-]+)$",
                facility_line,
            )
            if mm:
                po.ship_to_name = mm.group("name").strip()
                po.buyer_name   = mm.group("buyer").strip()
            else:
                po.ship_to_name = facility_line
        # Receiving address city/state -- search next 3 lines for the first
        # "<City>, <ST>, <ZIP>" match (the BUYER's address sits later in
        # the same line, but the FACILITY city is the first match).
        for j in range(idx + 2, min(idx + 5, len(lines))):
            city_m = _ADDR_CITY_RE.match(lines[j].strip())
            if city_m:
                po.ship_to_city  = city_m.group(1).upper()
                po.ship_to_state = city_m.group(2)
                po.ship_to_zip   = city_m.group(3)
                break
        break

    # Map warehouse: ship-to ID wins; fall back to city.
    if po.ship_to_id in CW_SHIP_TO_TO_WAREHOUSE:
        po.warehouse = CW_SHIP_TO_TO_WAREHOUSE[po.ship_to_id]
    elif po.ship_to_city in CW_DC_CITY_TO_WAREHOUSE:
        po.warehouse = CW_DC_CITY_TO_WAREHOUSE[po.ship_to_city]

    # Parse line items. Each item is a 2-line block. CW prints all
    # blocks back-to-back at the end of the PO.
    expected_ln = 1
    i = 0
    while i < len(lines):
        head_m = _LINE_HEAD_RE.match(lines[i])
        # A line-item head only counts when the cw_item starts with "1050"
        # (CW H&H bagel SKU prefix) -- filters out anything else that
        # happens to match the LN/spaces pattern.
        if head_m and head_m.group(2).startswith("1050"):
            prefix  = head_m.group(1)
            cw_item = head_m.group(2)
            desc    = head_m.group(3).strip()

            # Disambiguate prefix into (line_no, vendor_item):
            # if it starts with the expected line number, the remainder
            # is the vendor item (or empty).
            line_no = expected_ln
            vendor_item = ""
            if prefix == str(expected_ln):
                vendor_item = ""
            elif prefix.startswith(str(expected_ln)):
                vendor_item = prefix[len(str(expected_ln)):]
            else:
                # Mismatch -- fall back to single-digit line# heuristic.
                line_no = int(prefix[0]) if prefix and prefix[0].isdigit() else expected_ln
                vendor_item = prefix[1:] if len(prefix) > 1 else ""

            # The detail line should be the next non-blank line.
            detail_m = None
            for j in range(i + 1, min(i + 4, len(lines))):
                detail_m = _LINE_DETAIL_RE.match(lines[j].strip())
                if detail_m:
                    break
            if detail_m:
                try:
                    qty = float(detail_m.group("qty").replace(",", ""))
                except ValueError:
                    qty = 0.0
                try:
                    unit_cost = float(detail_m.group("unit_cost").replace(",", ""))
                except ValueError:
                    unit_cost = None
                try:
                    ext_cost = float(detail_m.group("ext_cost").replace(",", ""))
                except ValueError:
                    ext_cost = None
                pack = detail_m.group("pack")
                pack_um = detail_m.group("pack_um")
                qty_um = detail_m.group("qty_um") or "CS"
            else:
                qty, unit_cost, ext_cost = 0.0, None, None
                pack, pack_um, qty_um = "", "", "CS"

            variety = CW_DESCRIPTION_TO_VARIETY.get(desc.upper(), "")
            sliced = desc.upper().endswith(" SLICED") or cw_item.endswith("SLD")
            if not variety:
                po.unmapped_descriptions.append(desc)

            po.lines.append(ChefsWarehousePOLine(
                line_no    = line_no,
                vendor_item= vendor_item,
                cw_item    = cw_item,
                description= desc,
                variety    = variety,
                sliced     = sliced,
                pack       = pack,
                pack_um    = pack_um,
                case_size  = _case_size_from_pack(pack),
                quantity   = qty,
                quantity_um= qty_um,
                unit_cost  = unit_cost,
                ext_cost   = ext_cost,
            ))
            expected_ln = line_no + 1
        i += 1

    return po


def total_cs(po: ChefsWarehousePO) -> float:
    """Sum case quantity across line items."""
    return sum(l.quantity for l in po.lines
               if (l.quantity_um or "").upper() == "CS")


def dc_code_from_subject(subject: str) -> str:
    """Pull the DC tag (NY/MD/FLA/CHI/...) out of an email subject like
    'PO 1087421 FLA' or 'CW ORDER PO 1068886 MD'. Returns '' when no
    trailing all-caps token is present."""
    if not subject:
        return ""
    # Last whitespace-separated token of the subject, stripped of trailing
    # punctuation. CW's subjects are stable (PO #, DC tag), but the tag is
    # sometimes preceded by extra spaces.
    parts = re.findall(r"[A-Z]{2,4}", subject.upper())
    return parts[-1] if parts else ""


__all__ = [
    "ChefsWarehousePO",
    "ChefsWarehousePOLine",
    "CW_SHIP_TO_TO_WAREHOUSE",
    "CW_DC_CITY_TO_WAREHOUSE",
    "CW_DESCRIPTION_TO_VARIETY",
    "parse_po_pdf",
    "parse_po_text",
    "total_cs",
    "dc_code_from_subject",
]
