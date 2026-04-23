"""US Foods PO PDF parser.

US Foods submits purchase orders to H&H Bagels as PDF attachments on emails
from ``NORTHEASTCONFIRMATIONS.SHARED@USFOODS.COM``. The email body is a
boilerplate confirmation request — the actual line items live in the
attached PDF (typical filename: ``US Foods PO Request - <po#> - Date
<mmyyyy>.PDF``).

This module parses that PDF into a structured ``UsFoodsPO`` dataclass so the
email scanner can emit ``restock`` events per line item.

Dependencies:
    pypdf>=4.0    # for text extraction (no image / OCR needed; USF POs
                  # are text-based)

Usage:
    with open("po.pdf", "rb") as f:
        po = parse_po_pdf(f.read())
    for line in po.lines:
        print(line.usf_item_no, line.variety, line.quantity)

PDF layout (fixed-width, text-based):
    PURCHASE ORDER NO. <po> <revision>
    ORDER DATE: mm/dd/yy   ... SCHEDULE SHIPMENT TO ARRIVE ON: mm/dd/yy
    VENDOR 150345
    Header block has mail-to on the left and ship-to on the right on the
    same lines, e.g.:
        " QUEENS   NY 11377     MANASSAS   VA 20109   ---- R E M A R K S ----"
    Line items (two lines per item):
              <scc/gtin>  <pack>  -<label> <mfr_prod_no>
      <qty> CASES  <usf_item_no>  BAGEL, <desc>   <list_cost>  ...  <net_cost>

Unknown USF item numbers are surfaced in ``UsFoodsPO.unmapped_items`` rather
than silently dropped, so the sync layer can flag them.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# USF item # → canonical H&H variety name.
#
# The H&H seed list uses 11 varieties (see seed_bagels.py). USF's PO uses
# short 4-digit item codes. The "cheese wheat" variants are mapped back onto
# the existing Asiago and Jalapeño Cheddar SKUs per JD's instruction (they
# are wheat-flour variants of the same SKU, not new SKUs).
#
# Poppy Seed (1152) is NOT in the current seed list — mapping it here still
# so downstream code can surface it clearly; the inventory match will fail
# until a Poppy Seed SKU is added.
# ---------------------------------------------------------------------------
USF_ITEM_TO_VARIETY: dict[str, str] = {
    "1150": "Plain",
    "1152": "Poppy Seed",          # not in current seed — will need an SKU
    "1153": "Sesame",
    "1158": "Everything",
    "1159": "Asiago",              # from "ASIGO CHS WHEAT" — mapped per JD
    "1184": "Egg",
    "1189": "Jalapeno Cheddar",    # from "JLP CHEDR CHS WHEAT" — mapped per JD
}

# City (uppercase) seen on USF ship-to labels -> canonical "<City>, <ST>"
# used by seed_bagels.py. Extend as new DCs appear on POs.
USF_DC_CITY_TO_WAREHOUSE: dict[str, str] = {
    "MANASSAS":  "Manassas, VA",
    "ZEBULON":   "Zebulon, NC",
    "LA MIRADA": "La Mirada, CA",
    "CHICAGO":   "Chicago, IL",
    "ALCOA":     "Alcoa, TN",
}

# Pack size → count of units per case. USF uses two patterns on H&H POs:
#   6/10/4.06   = 6 sleeves × 10 bagels × 4.06 oz = 60 bagels/case
#   10/6/4.25   = 10 sleeves × 6 bagels × 4.25 oz = 60 bagels/case
_PACK_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*/\s*[\d.]+\s*$")


def _case_size_from_pack(pack: str) -> Optional[int]:
    m = _PACK_RE.match(pack or "")
    if not m:
        return None
    try:
        return int(m.group(1)) * int(m.group(2))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class UsFoodsPOLine:
    """One line item on a US Foods PO."""
    usf_item_no: str
    quantity: float
    unit: str                     # "CASES", etc.
    description: str
    list_cost: Optional[float]
    net_cost: Optional[float]
    scc_gtin: Optional[str]
    pack: Optional[str]           # e.g. "6/10/4.06"
    mfr_prod_no: Optional[str]
    variety: Optional[str]        # canonical H&H variety (None = unmapped)
    case_size: Optional[int]      # units per case (derived from pack)


@dataclass
class UsFoodsPO:
    """Parsed US Foods purchase order."""
    po_number: str = ""
    po_revision: str = ""
    order_date: str = ""          # mm/dd/yy as printed
    cancel_date: str = ""
    arrive_date: str = ""
    vendor_number: str = ""       # USF's code for H&H, e.g. "150345"
    buyer: str = ""
    ship_to_city: str = ""
    ship_to_state: str = ""
    ship_to_zip: str = ""
    warehouse: Optional[str] = None   # canonical "Manassas, VA" or None if DC is new
    lines: list = field(default_factory=list)
    unmapped_items: list = field(default_factory=list)   # USF item #s not in USF_ITEM_TO_VARIETY
    raw_text: str = ""            # retained for debugging


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------

def _extract_text(pdf_bytes: bytes) -> str:
    """Extract text from a PDF. Handles single or multi-page POs."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "pypdf is required for US Foods PO parsing. "
            "Add `pypdf>=4.0` to requirements.txt and `pip install` it."
        ) from e

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


# ---------------------------------------------------------------------------
# Regex building blocks
# ---------------------------------------------------------------------------

_PO_HEADER_RE = re.compile(
    r"PURCHASE\s+ORDER\s+NO\.\s+(?P<po>\S+)\s+(?P<rev>\S+)",
    re.IGNORECASE,
)
_ORDER_DATE_RE = re.compile(
    r"ORDER\s+DATE:\s*(?P<order>\d{2}/\d{2}/\d{2})"
    r"\s+CANCEL\s+DATE:\s*(?P<cancel>\d{2}/\d{2}/\d{2})",
    re.IGNORECASE,
)
_ARRIVE_RE = re.compile(
    r"SCHEDULE\s+SHIPMENT\s+TO\s+ARRIVE\s+ON:\s*(?P<arrive>\d{2}/\d{2}/\d{2})",
    re.IGNORECASE,
)
_VENDOR_RE = re.compile(r"\bVENDOR\s+(?P<vendor>\d+)\b")
_BUYER_RE = re.compile(r"BUYER:\s*\d+\s*(?P<buyer>[A-Z][A-Z \.]+)")

# "CITY  ST 12345" — used with findall so we can pick the RIGHTMOST match
# on the address line. Mail-to is on the left, ship-to on the right.
_CITY_ST_ZIP_RE = re.compile(
    r"([A-Z][A-Z ]{1,24}?)\s{2,}([A-Z]{2})\s+(\d{5})(?=\s|$)"
)

# Line 1 of an item block:
#   "              10859313006226  6/10/4.06   -H&HBAGELS  1055010"
_ITEM_LINE1_RE = re.compile(
    r"^\s+(?P<scc>\d{12,14})\s+(?P<pack>\d+/\d+/[\d.]+)\s+-\S+\s+(?P<mfr>\d+)\s*$"
)
# Line 2 of an item block:
#   "    8 CASES   1184            BAGEL, EGG 4.06 Z UNSL HEAT &        27.00                          27.00"
_ITEM_LINE2_RE = re.compile(
    r"^\s*(?P<qty>\d+(?:\.\d+)?)\s+(?P<unit>CASES|EACH|LBS|CS)\s+"
    r"(?P<item>\d+)\s+(?P<desc>.+?)\s+"
    r"(?P<list>\d+\.\d+)"
    r"(?:\s+(?P<off>\d+\.\d+))?"
    r"(?:\s+(?P<frt>\d+\.\d+))?"
    r"\s+(?P<net>\d+\.\d+)\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Main parse entrypoints
# ---------------------------------------------------------------------------

def parse_po_pdf(pdf_bytes: bytes) -> UsFoodsPO:
    """Parse a US Foods PO PDF into a structured object."""
    return parse_po_text(_extract_text(pdf_bytes))


def parse_po_text(text: str) -> UsFoodsPO:
    """Parse already-extracted PO text. Split out for unit testing."""
    po = UsFoodsPO(raw_text=text)

    if m := _PO_HEADER_RE.search(text):
        po.po_number = m.group("po")
        po.po_revision = m.group("rev")
    if m := _ORDER_DATE_RE.search(text):
        po.order_date = m.group("order")
        po.cancel_date = m.group("cancel")
    if m := _ARRIVE_RE.search(text):
        po.arrive_date = m.group("arrive")
    if m := _VENDOR_RE.search(text):
        po.vendor_number = m.group("vendor")
    if m := _BUYER_RE.search(text):
        po.buyer = m.group("buyer").strip()

    # Ship-to: scan lines between the "S H I P   T O" header and the items
    # table. Use findall and keep the rightmost match per line because
    # mail-to and ship-to share the same line in a two-column layout.
    lines = text.splitlines()
    ship_to_idx = _find_section(lines, "S H I P   T O") or 0
    items_header_idx = _find_section(lines, "ORDER ORDER") or len(lines)
    for i in range(ship_to_idx, items_header_idx):
        matches = _CITY_ST_ZIP_RE.findall(lines[i])
        if not matches:
            continue
        city, st, zp = matches[-1]
        po.ship_to_city = city.strip()
        po.ship_to_state = st
        po.ship_to_zip = zp
    if po.ship_to_city:
        po.warehouse = USF_DC_CITY_TO_WAREHOUSE.get(po.ship_to_city.upper())

    # Line items: pair up Line1 + Line2.
    i = 0
    n = len(lines)
    while i < n:
        m1 = _ITEM_LINE1_RE.match(lines[i])
        if not m1:
            i += 1
            continue
        j = i + 1
        m2 = None
        while j < n and j <= i + 3:
            if lines[j].strip():
                m2 = _ITEM_LINE2_RE.match(lines[j])
                break
            j += 1
        if m2 is None:
            i += 1
            continue

        usf_item = m2.group("item")
        variety = USF_ITEM_TO_VARIETY.get(usf_item)
        if variety is None:
            po.unmapped_items.append(usf_item)

        pack = m1.group("pack")
        po.lines.append(UsFoodsPOLine(
            usf_item_no=usf_item,
            quantity=float(m2.group("qty")),
            unit=m2.group("unit").upper(),
            description=m2.group("desc").strip(),
            list_cost=_opt_float(m2.group("list")),
            net_cost=_opt_float(m2.group("net")),
            scc_gtin=m1.group("scc"),
            pack=pack,
            mfr_prod_no=m1.group("mfr"),
            variety=variety,
            case_size=_case_size_from_pack(pack),
        ))
        i = j + 1

    return po


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _find_section(lines, needle: str) -> Optional[int]:
    """Return the first line index whose content contains `needle`
    (whitespace-insensitive)."""
    needle_compact = re.sub(r"\s+", "", needle).lower()
    for idx, line in enumerate(lines):
        if needle_compact in re.sub(r"\s+", "", line).lower():
            return idx
    return None


def _opt_float(v):
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


__all__ = [
    "UsFoodsPO",
    "UsFoodsPOLine",
    "USF_ITEM_TO_VARIETY",
    "USF_DC_CITY_TO_WAREHOUSE",
    "parse_po_pdf",
    "parse_po_text",
]
