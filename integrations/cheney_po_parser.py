"""Parser for Cheney Brothers PO PDF attachments.

Cheney POs arrive from NOREPLY@CHENEYBROTHERS.COM with a single PDF
attachment generically named PDF.PDF. The PDF is text-extractable
with pypdf. Body is empty; subject is "PO # <number>".

Layout (observed on PO 014511694485, Riviera Beach DC, April 2026):

    Information
    Purchase Order Number: 014511694485
    Order Date: 04/17/2026
    Vendor No: 9005338
    Vendor Name: H & H BAGELS
    Buyer: CINDY VARGAS
    Delivery Date 05/08/2026
    Pickup/Delivery: Delivery

    Shipping Address RIVIERA FACILITY - CBI
    ONE CHENEY WAY
    RIVIERA BEACH FL  33404
    USA

    Item Material/Description Brand Pack Size Quantity UM Unit Price  Net Amount
       10 10153019 BAGEL POPPY PARBAKED H & H 001/60   CT     24 CS
                                              Mfg#   1152                GTIN#:

Line items come in 2-line blocks:
    Line 1: <pos> <cheney item#> <description> <brand> <pack> <pack UM> <qty> <qty UM>
    Line 2: Mfg# <H&H mfg code> GTIN#: <optional>

The Mfg# field is H&H's own internal SKU code - the same number USF
uses in their "item #" column. See integrations/hh_mfg_codes.py for
the shared variety map.

Pack "001/60" = 1 sleeve per case x 60 bagels = 60 per case.

PO numbers are preserved exactly as printed in the PDF (with leading
zeros). Cheney POs observed so far do NOT expose a revision marker,
so po_revision is left empty - revision-replace in sync_inventory
handles the empty-rev case by matching on po_number alone.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Optional

import pypdf

try:
    from .hh_mfg_codes import HH_MFG_CODE_TO_VARIETY
except ImportError:  # standalone / test use
    from hh_mfg_codes import HH_MFG_CODE_TO_VARIETY  # type: ignore


# ---------------------------------------------------------------------------
# Reference data: Cheney DCs mapped to the canonical "<City>, <ST>" strings
# used by seed_bagels.py. City is matched uppercase. Extend as new DCs appear.
# ---------------------------------------------------------------------------
CHENEY_DC_CITY_TO_WAREHOUSE: dict[str, str] = {
    "RIVIERA BEACH": "Riviera Beach, FL",
    "OCALA":         "Ocala, FL",
    "PUNTA GORDA":   "Punta Gorda, FL",
}

# Flat case cost for all H&H bagel SKUs sold to Cheney (Cheney PO PDFs
# don't include a cost column, so we apply this fallback per line).
CHENEY_CASE_COST: float = 26.50


# Pack sizes on H&H lines at Cheney look like "001/60" (one sleeve of 60).
_PACK_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def _case_size_from_pack(pack: str) -> Optional[int]:
    m = _PACK_RE.match(pack or "")
    if not m:
        return None
    try:
        return int(m.group(1)) * int(m.group(2))
    except ValueError:
        return None


@dataclass
class CheneyPOLine:
    position: str = ""
    cheney_item: str = ""         # Cheney's own catalog number (e.g. 10153019)
    description: str = ""          # e.g. "BAGEL POPPY PARBAKED"
    brand: str = ""                # always "H & H" for H&H POs
    pack: str = ""                 # e.g. "001/60"
    pack_um: str = ""              # e.g. "CT"
    quantity: float = 0.0
    quantity_um: str = ""          # e.g. "CS" (cases)
    mfg_code: str = ""             # H&H internal, e.g. "1152"
    gtin: str = ""
    variety: str = ""              # mapped via HH_MFG_CODE_TO_VARIETY
    case_size: Optional[int] = None
    net_cost: Optional[float] = None  # defaulted to CHENEY_CASE_COST on parse


@dataclass
class CheneyPO:
    po_number: str = ""            # as printed in PDF, leading zeros preserved
    po_revision: str = ""          # Cheney doesn't expose rev on this layout
    order_date: str = ""           # MM/DD/YYYY as printed
    delivery_date: str = ""        # MM/DD/YYYY as printed
    vendor_number: str = ""        # H&H's vendor # at Cheney
    vendor_name: str = ""
    buyer: str = ""
    ship_to_name: str = ""         # e.g. "RIVIERA FACILITY - CBI"
    ship_to_city: str = ""
    ship_to_state: str = ""
    ship_to_zip: str = ""
    warehouse: str = ""            # canonical "<City>, <ST>"
    lines: list = field(default_factory=list)
    unmapped_items: list = field(default_factory=list)  # mfg codes not in HH_MFG_CODE_TO_VARIETY

    @property
    def total_cases(self) -> float:
        return sum(l.quantity for l in self.lines
                   if l.quantity_um.upper() == "CS")


def _extract_text(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def parse_po_pdf(pdf_bytes: bytes) -> CheneyPO:
    return parse_po_text(_extract_text(pdf_bytes))


# ---------------------------------------------------------------------------
# Header / metadata regexes (single-line anchors)
# ---------------------------------------------------------------------------
_PO_NUMBER_RE    = re.compile(r"Purchase Order Number:\s*(\S+)")
_ORDER_DATE_RE   = re.compile(r"Order Date:\s*(\S+)")
_DELIV_DATE_RE   = re.compile(r"Delivery Date\s+(\d{1,2}/\d{1,2}/\d{2,4})")
_VENDOR_NO_RE    = re.compile(r"Vendor No:\s*(\S+)")
_VENDOR_NAME_RE  = re.compile(r"Vendor Name:\s*(.+)")
_BUYER_RE        = re.compile(r"^Buyer:\s*(.+?)\s*$", re.MULTILINE)

# Shipping address is a multi-line block introduced by "Shipping Address ",
# then a facility name, then street, then "<CITY> <ST>  <ZIP>".
_SHIP_BLOCK_RE = re.compile(
    r"Shipping Address\s+(?P<name>.+?)\n"
    r"(?P<street>.+?)\n"
    r"(?P<city>[A-Z][A-Z .&'-]+?)\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?\s*\n"
    r"USA",
    re.MULTILINE,
)

# Line-item head: position, 6-10 digit cheney item, description, brand H&H,
# pack "nnn/nn", pack UM (2-4 letters), quantity, quantity UM.
_LINE_HEAD_RE = re.compile(
    r"^\s{2,}(?P<pos>\d+)\s+"
    r"(?P<cheney>\d{6,10})\s+"
    r"(?P<desc>.+?)\s+"
    r"(?P<brand>H\s*&\s*H)\s+"
    r"(?P<pack>\d{3}/\d+)\s+"
    r"(?P<pack_um>[A-Z]{2,4})\s+"
    r"(?P<qty>[\d,.]+)\s+"
    r"(?P<qty_um>[A-Z]{2,4})\s*$"
)

# Mfg# line follows the head line (may have trailing GTIN#).
_MFG_RE = re.compile(r"Mfg#\s+(?P<mfg>\S+)(?:\s+GTIN#:\s*(?P<gtin>\S*))?")


def parse_po_text(text: str) -> CheneyPO:
    po = CheneyPO()

    if m := _PO_NUMBER_RE.search(text):      po.po_number     = m.group(1).strip()
    if m := _ORDER_DATE_RE.search(text):     po.order_date    = m.group(1).strip()
    if m := _DELIV_DATE_RE.search(text):     po.delivery_date = m.group(1).strip()
    if m := _VENDOR_NO_RE.search(text):      po.vendor_number = m.group(1).strip()
    if m := _VENDOR_NAME_RE.search(text):    po.vendor_name   = m.group(1).strip()
    if m := _BUYER_RE.search(text):          po.buyer         = m.group(1).strip()

    # Ship-to block
    if m := _SHIP_BLOCK_RE.search(text):
        po.ship_to_name  = m.group("name").strip()
        po.ship_to_city  = m.group("city").strip()
        po.ship_to_state = m.group("state")
        po.ship_to_zip   = m.group("zip")
        po.warehouse     = CHENEY_DC_CITY_TO_WAREHOUSE.get(po.ship_to_city.upper(), "")

    # Line items: iterate lines, on each head match, look ahead up to 3 lines
    # for a Mfg# anchor (usually the next line).
    lines = text.splitlines()
    for idx, raw in enumerate(lines):
        head = _LINE_HEAD_RE.match(raw)
        if not head:
            continue
        mfg_code, gtin = "", ""
        for j in range(idx + 1, min(idx + 4, len(lines))):
            mm = _MFG_RE.search(lines[j])
            if mm:
                mfg_code = (mm.group("mfg") or "").strip()
                gtin     = (mm.group("gtin") or "").strip()
                break

        line = CheneyPOLine(
            position     = head.group("pos"),
            cheney_item  = head.group("cheney"),
            description  = head.group("desc").strip(),
            brand        = "H & H",
            pack         = head.group("pack"),
            pack_um      = head.group("pack_um"),
            quantity     = float(head.group("qty").replace(",", "")),
            quantity_um  = head.group("qty_um"),
            mfg_code     = mfg_code,
            gtin         = gtin,
            case_size    = _case_size_from_pack(head.group("pack")),
            net_cost     = CHENEY_CASE_COST,
        )
        line.variety = HH_MFG_CODE_TO_VARIETY.get(mfg_code, "")
        if not line.variety and mfg_code:
            po.unmapped_items.append(mfg_code)
        po.lines.append(line)

    return po


__all__ = [
    "CheneyPO",
    "CheneyPOLine",
    "CHENEY_DC_CITY_TO_WAREHOUSE",
    "CHENEY_CASE_COST",
    "parse_po_pdf",
    "parse_po_text",
]
