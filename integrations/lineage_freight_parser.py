"""Parser for Lineage Freight Management LLC invoice PDFs.

Lineage emails arrive as a ``Billable Invoice(s) from LINEAGE FREIGHT
MANAGEMENT LLC`` message from ``noreply@tms.blujaysolutions.net`` with a
single ``BillableBatch_<id>.zip`` attachment that unpacks to one or more
``Billable_Invoice_<invoice#>.pdf`` files.

Each PDF is one shipment / one invoice. Layout (single page):

    Freight Invoice
    Invoice #: 7020657208
    Invoice Date: 05/27/2026
    Shipment ID: 1022507        Ship Date: 05/15/2026
    Pick-up:    H&H BAGELS, WOODSIDE, NY 11377
    Consignee:  CHEFS WAREHOUSE
                7477 CANDLEWOOD RD.
                HANOVER, MD 21076
    Reference Numbers: SHIPPER REF #: 1022507, ORDER #: 1022507, PO: 1095389
    BASIS ITEM     FLT 1.00   608.5800   608.58
    FUEL SURCHARGE FLT 1.00   319.5000   319.50
    Weight 2,300   Volume 0   Pallets 2   TOTAL DUE 928.08 USD
    Distance(mi) 219   Cases 112

We extract the structured fields plus a normalised destination
("dest_dc": one of the H&H DCs we track, e.g. "Manassas, VA") so the
freight tab can group spend by warehouse.

The parser is dependency-light: pypdf for text extraction (already in
requirements.txt) + stdlib re. No pdfplumber dependency.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

try:
    from pypdf import PdfReader  # type: ignore
except Exception:  # pragma: no cover
    PdfReader = None  # type: ignore


# ---------------------------------------------------------------------------
# Destination normalisation
# ---------------------------------------------------------------------------
# Map Lineage consignee strings -> ("<City>, <ST>", "<canonical distributor>").
# Order matters: more-specific matches first. The canonical DC names mirror
# the labels in seed_bagels.WAREHOUSES so freight records line up with the
# inventory warehouses 1:1.

# (lineage_substring, dest_dc, distributor)
_DC_PATTERNS: list[tuple[str, str, str]] = [
    # US Foods
    ("USF MANASSAS",                 "Manassas, VA",      "US Foods"),
    ("US FOODS - MANASSAS",          "Manassas, VA",      "US Foods"),
    ("US FOODSERVICE - MANASSAS",    "Manassas, VA",      "US Foods"),
    ("USF LA MIRADA",                "La Mirada, CA",     "US Foods"),
    ("US FOODS - LA MIRADA",         "La Mirada, CA",     "US Foods"),
    ("US FOODSERVICE - LA MIRADA",   "La Mirada, CA",     "US Foods"),
    ("USF - CHICAGO",                "Chicago, IL",       "US Foods"),
    ("US FOODS - CHICAGO",           "Chicago, IL",       "US Foods"),
    ("USF CHICAGO",                  "Chicago, IL",       "US Foods"),
    ("US FOODS - RALEIGH",           "Zebulon, NC",       "US Foods"),
    ("US FOODSERVICE - RALEIGH",     "Zebulon, NC",       "US Foods"),
    ("US FOODS RALEIGH",             "Zebulon, NC",       "US Foods"),
    ("USF RALEIGH",                  "Zebulon, NC",       "US Foods"),
    ("USF ZEBULON",                  "Zebulon, NC",       "US Foods"),
    ("US FOODS - ALCOA",             "Alcoa, TN",         "US Foods"),
    ("USF ALCOA",                    "Alcoa, TN",         "US Foods"),
    # Generic "US FOODS" / "US FOOD SERVICE" — fall back to state-from-zip
    # below; we set the distributor here but leave dest_dc empty so the
    # post-processing block can fill it in.
    ("US FOODSERVICE",               "",                  "US Foods"),
    ("US FOOD SERVICE",              "",                  "US Foods"),
    ("US FOODS",                     "",                  "US Foods"),
    ("USF",                          "",                  "US Foods"),

    # Cheney Brothers (Riviera / Ocala / Punta Gorda)
    ("CHENEY BORTHERS - RIVIERA",    "Riviera Beach, FL", "Cheney Brothers"),
    ("CHENEY BROTHERS - RIVIERA",    "Riviera Beach, FL", "Cheney Brothers"),
    ("CHENEY BROS - RIVIERA",        "Riviera Beach, FL", "Cheney Brothers"),
    ("CHENEY BORTHERS - PUNTA",      "Punta Gorda, FL",   "Cheney Brothers"),
    ("CHENEY BROTHERS - PUNTA",      "Punta Gorda, FL",   "Cheney Brothers"),
    ("CHENEY BROS - PUNTA",          "Punta Gorda, FL",   "Cheney Brothers"),
    ("CHENEY BORTHERS - OCALA",      "Ocala, FL",         "Cheney Brothers"),
    ("CHENEY BROTHERS - OCALA",      "Ocala, FL",         "Cheney Brothers"),
    ("CHENEY BROS OCALA",            "Ocala, FL",         "Cheney Brothers"),
    ("CHENEY BROS",                  "",                  "Cheney Brothers"),
    ("CHENEY",                       "",                  "Cheney Brothers"),

    # Chefs Warehouse / Dairyland
    ("DAIRYLAND/CHEF'S WAREHOUSE",   "Bronx, NY",         "Chefs Warehouse"),
    ("DAIRYLAND/CHEFS WAREHOUSE",    "Bronx, NY",         "Chefs Warehouse"),
    ("DAIRYLAND BEL CANTO",          "Bronx, NY",         "Chefs Warehouse"),
    ("DAIRLYLAND BEL CANTO",         "Bronx, NY",         "Chefs Warehouse"),
    ("DAIRYLAND",                    "Bronx, NY",         "Chefs Warehouse"),
    ("DAIRLYLAND",                   "Bronx, NY",         "Chefs Warehouse"),
    ("CHEFS WAREHOUSE",              "Hanover, MD",       "Chefs Warehouse"),
    ("CHEF WAREHOUSE",               "Hanover, MD",       "Chefs Warehouse"),
    ("CHEFS' WAREHOUSE",             "Hanover, MD",       "Chefs Warehouse"),
    ("CHEF'S WAREHOUSE",             "Hanover, MD",       "Chefs Warehouse"),
]

# When the consignee name is too generic (e.g. just "US FOODS") we
# disambiguate by ZIP -> DC.
_ZIP_TO_DC: dict[str, tuple[str, str]] = {
    "20109": ("Manassas, VA",      "US Foods"),
    "27597": ("Zebulon, NC",       "US Foods"),
    "27591": ("Zebulon, NC",       "US Foods"),
    "90638": ("La Mirada, CA",     "US Foods"),
    "60106": ("Chicago, IL",       "US Foods"),
    "60515": ("Chicago, IL",       "US Foods"),
    "37701": ("Alcoa, TN",         "US Foods"),
    "33404": ("Riviera Beach, FL", "Cheney Brothers"),
    "34474": ("Ocala, FL",         "Cheney Brothers"),
    "34475": ("Ocala, FL",         "Cheney Brothers"),
    "33982": ("Punta Gorda, FL",   "Cheney Brothers"),
    "21076": ("Hanover, MD",       "Chefs Warehouse"),
    "10474": ("Bronx, NY",         "Chefs Warehouse"),
    "33054": ("Opa Locka, FL",     "Chefs Warehouse"),
    "60101": ("Chicago, IL",       "Chefs Warehouse"),
}


def _normalise_destination(consignee_name: str, lines: list) -> tuple:
    """Return (dest_dc, distributor). Empty strings when no match."""
    name = (consignee_name or "").upper().strip()
    for pat, dc, dist in _DC_PATTERNS:
        if pat in name:
            if dc:
                return dc, dist
            for ln in lines or []:
                m = re.search(r"\b([A-Z]{2})\s+(\d{5})\b", ln.upper())
                if m and m.group(2) in _ZIP_TO_DC:
                    dc2, _ = _ZIP_TO_DC[m.group(2)]
                    return dc2, dist
            return "", dist
    for ln in lines or []:
        m = re.search(r"\b([A-Z]{2})\s+(\d{5})\b", ln.upper())
        if m and m.group(2) in _ZIP_TO_DC:
            return _ZIP_TO_DC[m.group(2)]
    return "", ""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class FreightInvoice:
    """One Lineage freight invoice = one shipment from H&H to a DC."""
    invoice_number: str = ""
    invoice_date: str = ""        # ISO YYYY-MM-DD
    ship_date: str = ""           # ISO YYYY-MM-DD
    shipment_id: str = ""
    carrier: str = "Lineage Freight Management LLC"

    origin_name: str = ""
    origin_city: str = ""
    origin_state: str = ""
    origin_zip: str = ""

    consignee_name: str = ""
    consignee_city: str = ""
    consignee_state: str = ""
    consignee_zip: str = ""

    dest_dc: str = ""             # e.g. "Manassas, VA"
    distributor: str = ""         # "US Foods" | "Cheney Brothers" | "Chefs Warehouse"

    po_number: str = ""
    shipper_ref: str = ""
    order_number: str = ""

    total_due: float = 0.0
    currency: str = "USD"
    weight_lb: float = 0.0
    pallets: int = 0
    cases: int = 0
    distance_mi: float = 0.0
    line_items: list = field(default_factory=list)

    cost_per_pallet: float = 0.0
    cost_per_case: float = 0.0

    source: str = "lineage-email"
    source_message_id: str = ""
    source_subject: str = ""
    pdf_filename: str = ""
    ingested_at: str = ""

    def recompute_derived(self) -> None:
        self.cost_per_pallet = round(self.total_due / self.pallets, 2) if self.pallets else 0.0
        self.cost_per_case = round(self.total_due / self.cases, 4) if self.cases else 0.0


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------


def _pdf_text(pdf_bytes: bytes) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf is required to parse Lineage freight PDFs")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
    return "\n".join(parts)


def _parse_date(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


# Invoice # and Invoice Date have TWO observed layouts depending on the
# PDF text-extractor (pdfplumber vs pypdf):
#
#   pdfplumber: "Invoice #: 7020657208\nInvoice Date: 05/27/2026"
#   pypdf:      "Invoice Date:Invoice #:Freight Invoice\n7020657208\n05/27/2026\n..."
#
# The pdfplumber form fits the simple "Label: value" regex; the pypdf
# form puts ALL labels in one column and ALL values in the next, so we
# fall back to a "first big number" / "first date-shaped token after the
# `Freight Invoice` header" extractor.
_RE_INVOICE_NO_INLINE   = re.compile(r"Invoice\s*#:\s*([A-Za-z0-9\-]+)")
_RE_INVOICE_DATE_INLINE = re.compile(r"Invoice\s*Date:\s*([0-9/\-]+)")
_RE_SHIP_DATE           = re.compile(r"Ship\s*Date:\s*([0-9/\-]+)")
# Fallback patterns for pypdf's column-shifted layout. The 10-digit number
# starting with 70 is the invoice number, immediately followed on the next
# line by an MM/DD/YYYY-formatted invoice date.
_RE_PYPDF_HEADER = re.compile(
    r"Invoice\s*#:\s*Freight\s*Invoice\s*\n\s*(\d{6,12})\s*\n\s*(\d{1,2}/\d{1,2}/\d{2,4})"
)
_RE_SHIPMENT_ID  = re.compile(r"Shipment\s*ID:\s*(\S+)")
_RE_TOTAL_DUE    = re.compile(r"TOTAL\s*D\s*U?\s*E?\s*([\d,]+\.\d{2})\s*(USD|US\$|\$)?",
                              re.IGNORECASE)
_RE_PALLETS      = re.compile(r"Pallets\s*([\d,]+)")
_RE_CASES        = re.compile(r"Cases\s*([\d,]+)")
_RE_WEIGHT       = re.compile(r"Weight\s*([\d,]+)")
_RE_DISTANCE     = re.compile(r"Distance\s*\(\s*m\s*i\s*\)\s*([\d,]+)")
_RE_PO           = re.compile(r"\bPO:\s*([A-Z0-9\-]+)", re.IGNORECASE)
_RE_SHIPREF      = re.compile(r"SHIPPER\s*REF\s*#:\s*([^,\n]+)", re.IGNORECASE)
_RE_ORDER_NO     = re.compile(r"\bORDER\s*#:\s*([A-Z0-9\-]+)", re.IGNORECASE)


def _extract_block(text: str, start_label: str, end_label: str) -> list:
    pat = re.compile(re.escape(start_label) + r"\s*:?\s*(.+?)(?=" + re.escape(end_label) + r")",
                     re.DOTALL | re.IGNORECASE)
    m = pat.search(text)
    if not m:
        return []
    return [ln.strip() for ln in m.group(1).split("\n") if ln.strip()]


def parse_freight_pdf(pdf_bytes: bytes,
                      *,
                      pdf_filename: str = "",
                      source_message_id: str = "",
                      source_subject: str = "") -> Optional[FreightInvoice]:
    """Parse a single Lineage freight invoice PDF. Returns None if the PDF
    doesn't look like a Lineage freight invoice."""
    text = _pdf_text(pdf_bytes)
    if "Lineage" not in text and "Freight Invoice" not in text:
        return None

    inv = FreightInvoice(
        pdf_filename=pdf_filename,
        source_message_id=source_message_id,
        source_subject=source_subject,
        ingested_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    # Try pypdf column-shifted layout first (more specific), then fall
    # back to the simpler pdfplumber-style "Label: value" form.
    m = _RE_PYPDF_HEADER.search(text)
    if m:
        inv.invoice_number = m.group(1).strip()
        inv.invoice_date = _parse_date(m.group(2))
    else:
        m = _RE_INVOICE_NO_INLINE.search(text)
        if m:
            cand = m.group(1).strip().rstrip(":")
            # Guard against pypdf giving us "Freight" — bail to other path.
            if cand.lower() not in ("freight", "invoice"):
                inv.invoice_number = cand
        m = _RE_INVOICE_DATE_INLINE.search(text)
        if m: inv.invoice_date = _parse_date(m.group(1))
    m = _RE_SHIP_DATE.search(text)
    if m: inv.ship_date = _parse_date(m.group(1))
    m = _RE_SHIPMENT_ID.search(text)
    if m: inv.shipment_id = m.group(1).strip()

    m = _RE_PO.search(text)
    if m: inv.po_number = m.group(1).strip()
    m = _RE_SHIPREF.search(text)
    if m: inv.shipper_ref = m.group(1).strip()
    m = _RE_ORDER_NO.search(text)
    if m: inv.order_number = m.group(1).strip()

    pickup_lines = _extract_block(text, "Pick-up", "Consignee")
    if pickup_lines:
        inv.origin_name = pickup_lines[0]
        for ln in pickup_lines[1:]:
            zm = re.search(r"^(.*?),\s*([A-Z]{2})\s+(\d{5})", ln.upper())
            if zm:
                inv.origin_city = zm.group(1).title().strip()
                inv.origin_state = zm.group(2)
                inv.origin_zip = zm.group(3)
                break

    consignee_lines = _extract_block(text, "Consignee", "Reference Numbers")
    if consignee_lines:
        inv.consignee_name = consignee_lines[0]
        for ln in consignee_lines[1:]:
            zm = re.search(r"^(.*?),\s*([A-Z]{2})\s+(\d{5})", ln.upper())
            if zm:
                inv.consignee_city = zm.group(1).title().strip()
                inv.consignee_state = zm.group(2)
                inv.consignee_zip = zm.group(3)
                break
        inv.dest_dc, inv.distributor = _normalise_destination(
            inv.consignee_name, consignee_lines)

    m = _RE_TOTAL_DUE.search(text)
    if m: inv.total_due = float(m.group(1).replace(",", ""))
    m = _RE_PALLETS.search(text)
    if m:
        try: inv.pallets = int(m.group(1).replace(",", ""))
        except ValueError: pass
    m = _RE_CASES.search(text)
    if m:
        try: inv.cases = int(m.group(1).replace(",", ""))
        except ValueError: pass
    m = _RE_WEIGHT.search(text)
    if m:
        try: inv.weight_lb = float(m.group(1).replace(",", ""))
        except ValueError: pass
    m = _RE_DISTANCE.search(text)
    if m:
        try: inv.distance_mi = float(m.group(1).replace(",", ""))
        except ValueError: pass

    line_pat = re.compile(
        r"^([A-Z][A-Z\s\-/]+?)\s+([A-Z]{2,4})\s+([\d.]+)\s+([\d,]+\.\d{2,4})\s+([\d,]+\.\d{2})\s*$",
        re.MULTILINE,
    )
    for m in line_pat.finditer(text):
        desc, basis, qty, rate, ltot = m.group(1).strip(), m.group(2), m.group(3), m.group(4), m.group(5)
        if desc.upper() in ("TOTAL", "PAGE", "QUANTITY"):
            continue
        try:
            inv.line_items.append({
                "description": desc,
                "basis": basis,
                "qty": float(qty),
                "rate": float(rate.replace(",", "")),
                "total": float(ltot.replace(",", "")),
            })
        except ValueError:
            continue

    inv.recompute_derived()

    if not inv.invoice_number:
        return None
    return inv
