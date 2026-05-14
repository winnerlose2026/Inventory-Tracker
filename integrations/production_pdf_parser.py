"""Parser for H&H Bagels "Daily Production Sheet" PDFs.

These PDFs are sent internally (typically from gabo@hhbagels.com to
isaiah@hhbagels.com) with a subject like

    Daily Production RIVIERA BEACH.PO.014511697480

The attachment is one page of mostly tabular text rendered from a
template. Layout, in order of appearance (extracted via pypdf):

    Production Date
    CS Count SKU
    <qty> CS <variety>            (one row per line item)
    ...
    <total> Total Cases<WAREHOUSE>.PO.<po_number>
    Lot#
    <lot_1>
    <lot_2>
    ...<production_date as MM/DD/YYYY>     (date is concatenated to one of the lots)
    <lot_N>

The header `<WAREHOUSE>.PO.<po_number>` is concatenated to the total-
cases line. The production date is concatenated to whichever lot row
landed at the right spot on the page. Both are recovered with regex
so layout drift doesn't break ingestion.

Special cases handled:

  * CWNY layout omits the "PARB-" prefix on variety names.
  * Subject format on US Foods Manassas was observed as
    "Daily production US FOODS MANASSAS PO.4363705O" — lowercase 'p'
    and an extra 'US FOODS' prefix on the warehouse. The parser pulls
    the warehouse + PO from the PDF body, not the subject, so the
    subject is informational only.
  * Some emails attach an OCR'd scan (image-only PDF). pypdf returns
    no text in that case; ``parse_production_pdf`` returns a partial
    result with ``error`` populated and an empty ``lines`` list so the
    operator can flag the message and re-request a text PDF.

Dependencies: ``pypdf>=4.0`` (already in requirements.txt for the USF
and Cheney PO parsers).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Optional


# Variety normalization. The PDFs use the production team's internal
# variety codes (PARB-... or bare names on CWNY). Map to our canonical
# SKU variety so downstream cross-referencing with on_order works.
_VARIETY_ALIASES: dict[str, str] = {
    "PARB-PLAIN":                "Plain",
    "PARB-EVERYTHING":           "Everything",
    "PARB-SESAME":               "Sesame",
    "PARB-POPPY":                "Poppy Seed",
    "PARB-POPPY SEED":           "Poppy Seed",
    "PARB-ONION":                "Onion",
    "PARB-WHOLE WHEAT":          "Whole Wheat",
    "PARB-WHOLE WHEAT EVERYTHING": "Whole Wheat Everything",
    "PARB-WHEAT EVERYTHING":     "Whole Wheat Everything",
    "PARB-EGG":                  "Egg",
    "PARB-BLUEBERRY":            "Blueberry",
    "PARB-ASIAGO":               "Asiago",
    "PARB-JALAPENO":             "Jalapeno Cheddar",
    "PARB-JALAPENO CHEDDAR":     "Jalapeno Cheddar",
    "PARB-CINN-RAISIN":          "Cinnamon Raisin",
    "PARB-CINNAMON RAISIN":      "Cinnamon Raisin",
    # CWNY bare names (no PARB- prefix)
    "PLAIN":                     "Plain",
    "EVERYTHING":                "Everything",
    "SESAME":                    "Sesame",
    "POPPY":                     "Poppy Seed",
    "ONION":                     "Onion",
    "WHOLE WHEAT":               "Whole Wheat",
    "EGG":                       "Egg",
    "BLUEBERRY":                 "Blueberry",
    "ASIAGO":                    "Asiago",
    "JALAPENO":                  "Jalapeno Cheddar",
    "CINN-RAISIN":               "Cinnamon Raisin",
}

# Warehouse normalization. The production sheet uses an UPPERCASE
# city-only label; map to the "<City>, <ST>" form we already use on
# on_order entries so cross-referencing works. CWNY is a separate
# distributor we hadn't tracked before this — Chefs Warehouse NY.
_WAREHOUSE_TO_CANONICAL: dict[str, tuple[str, str]] = {
    # warehouse_label_in_pdf -> (canonical_warehouse, distributor)
    "RIVIERA BEACH":         ("Riviera Beach, FL",      "Cheney Brothers"),
    "OCALA":                 ("Ocala, FL",              "Cheney Brothers"),
    "PUNTA GORDA":           ("Punta Gorda, FL",        "Cheney Brothers"),
    "MANASSAS":              ("Manassas, VA",           "US Foods"),
    "US FOODS MANASSAS":     ("Manassas, VA",           "US Foods"),
    "ZEBULON":               ("Zebulon, NC",            "US Foods"),
    "LA MIRADA":             ("La Mirada, CA",          "US Foods"),
    "CHICAGO":               ("Chicago, IL",            "US Foods"),
    "ALCOA":                 ("Alcoa, TN",              "US Foods"),
    "CWNY":                  ("Chefs Warehouse, NY",    "Chefs Warehouse"),
    "CWFL":                  ("Chefs Warehouse, FL",    "Chefs Warehouse"),
}


@dataclass
class ProductionLine:
    variety: str          # canonical variety name
    raw_variety: str      # exactly as it appeared on the sheet
    cs_count: int
    lot_number: str = ""


@dataclass
class ProductionSheet:
    production_date: str = ""         # ISO YYYY-MM-DD if parseable, else MM/DD/YYYY raw
    warehouse: str = ""               # canonical "City, ST"
    warehouse_raw: str = ""           # exactly as printed on the sheet
    distributor: str = ""             # inferred from warehouse
    po_number: str = ""
    lines: list = field(default_factory=list)
    total_cases: int = 0
    unmapped_varieties: list = field(default_factory=list)
    raw_text: str = ""
    error: str = ""


# --------------------------------------------------------------------------
# Regexes
# --------------------------------------------------------------------------

# "<qty> CS <variety>"  — variety may include hyphens and spaces, stops at
# end of line / before next number.
_LINE_RE = re.compile(
    r"^\s*(\d+)\s+CS\s+([A-Z][A-Z0-9\- ]+?)\s*$",
    re.MULTILINE,
)

# "<total> Total Cases<WAREHOUSE>.PO.<po_number>"
_HEADER_RE = re.compile(
    # "<total> Total Cases [<lot_digits>] <WAREHOUSE>.PO.<po_number>"
    # La Mirada renders an extra lot # between "Cases" and the warehouse
    # because of how pypdf merges adjacent text runs; absorb optional
    # leading digits before the warehouse name.
    r"(\d+)\s+Total\s+Cases\s*\d*\s*([A-Z][A-Z 0-9]*?)\s*\.\s*PO\s*\.\s*([A-Z0-9]+)",
    re.IGNORECASE,
)

# Date inline in the lot column, e.g. "11890430264/30/2026" -> "4/30/2026"
_DATE_RE = re.compile(r"(?<!\d)((?:0?[1-9]|1[0-2])/\d{1,2}/\d{4})(?!\d)|(?:^|\D)((?:0?[1-9]|1[0-2])/\d{1,2}/\d{4})(?!\d)")


# --------------------------------------------------------------------------
# Parse entrypoints
# --------------------------------------------------------------------------

def parse_production_pdf(pdf_bytes: bytes, subject: str = "") -> ProductionSheet:
    try:
        text = _extract_text(pdf_bytes)
    except Exception as exc:  # noqa: BLE001
        return ProductionSheet(error=f"PDF text extraction failed: {exc}")
    return parse_production_text(text, subject=subject)


def parse_production_text(text: str, subject: str = "") -> ProductionSheet:
    sheet = ProductionSheet(raw_text=text)
    if not text or not text.strip():
        sheet.error = "PDF contained no extractable text (image-only scan?)"
        return sheet

    # Header — warehouse + PO number from "<total> Total Cases<WAREHOUSE>.PO.<po#>"
    m = _HEADER_RE.search(text)
    if m:
        sheet.total_cases = int(m.group(1))
        sheet.warehouse_raw = m.group(2).strip()
        sheet.po_number = m.group(3).strip()
        canonical = _WAREHOUSE_TO_CANONICAL.get(sheet.warehouse_raw.upper())
        if canonical:
            sheet.warehouse, sheet.distributor = canonical
        else:
            # Fallback: any "CW<STATE>" code is Chefs Warehouse <STATE>.
            # Lets us handle new Chefs Warehouse regions (CWDC, CWTX, ...)
            # without an explicit entry in _WAREHOUSE_TO_CANONICAL.
            cw_match = re.match(r"^CW([A-Z]{2})$", sheet.warehouse_raw.upper())
            if cw_match:
                sheet.warehouse = f"Chefs Warehouse, {cw_match.group(1)}"
                sheet.distributor = "Chefs Warehouse"
            else:
                sheet.warehouse = sheet.warehouse_raw

    # Production date — prefer a slash-format MM/DD/YYYY anywhere in the
    # document (Riviera Beach / Punta Gorda / CWNY style). Fall back to
    # the earliest date encoded in the lot-number suffixes (La Mirada
    # style: lot 1184050726 -> 05/07/26 produced).
    md = _DATE_RE.search(text)
    if md:
        raw = md.group(1) or md.group(2)
        try:
            mm, dd, yyyy = raw.split("/")
            sheet.production_date = f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"
        except Exception:
            sheet.production_date = raw
    else:
        dates_from_lots = sorted(_dates_from_lot_numbers(text))
        if dates_from_lots:
            sheet.production_date = dates_from_lots[0]

    # Line items — "<qty> CS <variety>"
    seen_unknown = set()
    for match in _LINE_RE.finditer(text):
        cs = int(match.group(1))
        raw_v = match.group(2).strip()
        canonical = _VARIETY_ALIASES.get(raw_v.upper())
        if not canonical:
            seen_unknown.add(raw_v)
            canonical = raw_v.title()  # best-effort display fallback
        sheet.lines.append(ProductionLine(
            variety=canonical, raw_variety=raw_v, cs_count=cs,
        ))
    sheet.unmapped_varieties = sorted(seen_unknown)

    # If the header total didn't parse but we got lines, compute it.
    if not sheet.total_cases and sheet.lines:
        sheet.total_cases = sum(L.cs_count for L in sheet.lines)

    # Subject is informational — log it for debugging if the body lacked
    # a header but the subject carried the PO.
    if not sheet.po_number and subject:
        sm = re.search(r"PO[._]\s*([A-Z0-9]+)", subject, re.IGNORECASE)
        if sm:
            sheet.po_number = sm.group(1).strip()

    if not sheet.lines:
        sheet.error = (sheet.error or "") + " no line items detected".strip()

    return sheet


_DIGIT_RUN_RE = re.compile(r"\d{10,}")


def _dates_from_lot_numbers(text: str) -> set:
    """Return the set of ISO production dates encoded in lot numbers.

    Lot format observed: ``<4-digit item_code><MMDDYY>``, e.g. 1184050726
    means item 1184 produced 05/07/26. We can't rely on a single regex
    because the slash-format production date is sometimes GLUED onto the
    last lot in the same digit run (CWFL renders as 11580511265/11/2026,
    where the lot 1158051126 ends at the 10th char and the date "5/11"
    begins immediately). So instead we scan every digit run of 10+
    characters and check each 10-digit window for a valid trailing
    MMDDYY.
    """
    out = set()
    for m in _DIGIT_RUN_RE.finditer(text):
        run = m.group(0)
        # Slide a 10-char window across the run and try each as <item><date>
        for i in range(0, len(run) - 9):
            window = run[i:i+10]
            mm, dd, yy = window[4:6], window[6:8], window[8:10]
            try:
                mm_i, dd_i, yy_i = int(mm), int(dd), int(yy)
            except ValueError:
                continue
            if not (1 <= mm_i <= 12 and 1 <= dd_i <= 31):
                continue
            year = 2000 + yy_i if yy_i < 70 else 1900 + yy_i
            out.add(f"{year:04d}-{mm_i:02d}-{dd_i:02d}")
    return out


def _extract_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


__all__ = [
    "ProductionLine", "ProductionSheet",
    "parse_production_pdf", "parse_production_text",
]
