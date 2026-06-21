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
    "POPPY SEED":                "Poppy Seed",
    "ONION":                     "Onion",
    "WHOLE WHEAT":               "Whole Wheat",
    "WHOLEWHEAT":                "Whole Wheat",
    "EGG":                       "Egg",
    "BLUEBERRY":                 "Blueberry",
    "ASIAGO":                    "Asiago",
    "JALAPENO":                  "Jalapeno Cheddar",
    "JALAPENO CHEDDAR":          "Jalapeno Cheddar",
    "CINN-RAISIN":               "Cinnamon Raisin",
    "CINNAMON RAISIN":           "Cinnamon Raisin",
    # Short-hand abbreviations seen on hand-keyed production sheets.
    "WW":                        "Whole Wheat",
    "WWET":                      "Whole Wheat Everything",
    "WW EVERYTHING":             "Whole Wheat Everything",
    "WHOLE WHEAT EVERYTHING":    "Whole Wheat Everything",
    "WHOLEWHEAT EVERYTHING":     "Whole Wheat Everything",
    # PARB- prefix variants of the abbreviations.
    "PARB-WW":                   "Whole Wheat",
    "PARB-WWET":                 "Whole Wheat Everything",
    "PARB-WW EVERYTHING":        "Whole Wheat Everything",
    "PARB-WHOLEWHEAT":           "Whole Wheat",
    "PARB-WHOLEWHEAT EVERYTHING":"Whole Wheat Everything",
    "PARB-WW-ET":                "Whole Wheat Everything",
    "PARB-WW ET":                "Whole Wheat Everything",
    "WW-ET":                     "Whole Wheat Everything",
    "WW ET":                     "Whole Wheat Everything",
    # Common typos
    "EVERYTTHING":               "Everything",
    "EVERYTING":                 "Everything",
    "EVERYTHIING":               "Everything",
    "BLUBERRY":                  "Blueberry",
    # Other compact tags spotted in the wild.
    "PARB-PLN":                  "Plain",
    "PARB-EVT":                  "Everything",
    "PARB-EVTHG":                "Everything",
    "PARB-SES":                  "Sesame",
    "PARB-PPY":                  "Poppy Seed",
    "PARB-ON":                   "Onion",
    "PARB-CINN":                 "Cinnamon Raisin",
    "PARB-BB":                   "Blueberry",
    "PARB-JC":                   "Jalapeno Cheddar",
    "PARB-JLP":                  "Jalapeno Cheddar",
    "PARB-AS":                   "Asiago",
}

# Map of compact abbreviations applied AFTER stripping a "PARB-" prefix.
# Lets us treat the bare form and the parbaked form the same way without
# bloating _VARIETY_ALIASES with every cross-product.
_VARIETY_SHORTHAND: dict[str, str] = {
    "WW":     "Whole Wheat",
    "WWET":   "Whole Wheat Everything",
    "WW-ET":  "Whole Wheat Everything",
    "WW ET":  "Whole Wheat Everything",
    "PLN":    "Plain",
    "EVT":    "Everything",
    "EVTHG":  "Everything",
    "SES":    "Sesame",
    "PPY":    "Poppy Seed",
    "ON":     "Onion",
    "CINN":   "Cinnamon Raisin",
    "BB":     "Blueberry",
    "JC":     "Jalapeno Cheddar",
    "JLP":    "Jalapeno Cheddar",
    "AS":     "Asiago",
}


def _normalize_variety(raw: str) -> tuple[str, bool]:
    """Resolve a raw variety string from a production sheet to a
    canonical variety name.

    Returns ``(canonical, recognized)``. If the raw value can't be
    mapped to anything we know about, the canonical is set to
    ``"In-House Inventory"`` so the ranking aggregates the noise
    into a single bucket instead of fragmenting it into one-off
    entries per typo.

    Resolution order:
      1. Exact match against ``_VARIETY_ALIASES`` (upper-cased).
      2. Strip a leading ``PARB``/``PARB-``/``PARB ``/``PRBKD`` prefix
         and re-try ``_VARIETY_ALIASES``.
      3. Look up the stripped form in ``_VARIETY_SHORTHAND``.
      4. Fall back to ``"In-House Inventory"``.
    """
    if not raw:
        return ("In-House Inventory", False)
    key = raw.strip().upper()
    if key in _VARIETY_ALIASES:
        return (_VARIETY_ALIASES[key], True)
    # Strip Parb-* / Parbaked / PRBKD prefixes
    stripped = key
    for pre in ("PARB-", "PARB ", "PARB.", "PARBAKED ", "PARBAKED-",
                "PRBKD-", "PRBKD "):
        if stripped.startswith(pre):
            stripped = stripped[len(pre):].strip()
            break
    if stripped != key:
        if stripped in _VARIETY_ALIASES:
            return (_VARIETY_ALIASES[stripped], True)
    if stripped in _VARIETY_SHORTHAND:
        return (_VARIETY_SHORTHAND[stripped], True)
    return ("In-House Inventory", False)

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
    "CWF":                   ("Chefs Warehouse, FL",    "Chefs Warehouse"),
    "CWC":                   ("Chefs Warehouse, Chicago",      "Chefs Warehouse"),
    "CWMID":                 ("Chefs Warehouse, Mid-Atlantic", "Chefs Warehouse"),
    # Chefs Warehouse PDF header sometimes prints hyphenated codes.
    # Subject format is "CW-MID-ATLANTIC", but pypdf occasionally extracts
    # a truncated "CW-MID-ATL" or "CW-MID" from the header — alias them
    # all to the same canonical destination so warehouse + distributor
    # populate correctly regardless of which form the layout produced.
    "CW-MID-ATLANTIC":       ("Chefs Warehouse, Mid-Atlantic", "Chefs Warehouse"),
    "CW-MID-ATL":            ("Chefs Warehouse, Mid-Atlantic", "Chefs Warehouse"),
    "CW-MID":                ("Chefs Warehouse, Mid-Atlantic", "Chefs Warehouse"),
    "CW-NY":                 ("Chefs Warehouse, NY",            "Chefs Warehouse"),
    "CW-FL":                 ("Chefs Warehouse, FL",            "Chefs Warehouse"),
    "CW-C":                  ("Chefs Warehouse, Chicago",       "Chefs Warehouse"),
    # H&H's production sheet uses "JACKSON VILLE" (two words) for the
    # production destined to Cheney's Ocala DC — per JD, route it there.
    "JACKSON VILLE":         ("Ocala, FL",              "Cheney Brothers"),
    "JACKSONVILLE":          ("Ocala, FL",              "Cheney Brothers"),
    # Common typos seen on hand-keyed sheets — route them like the
    # correctly-spelled originals so the operator doesn't have to.
    "MNASSAS":               ("Manassas, VA",           "US Foods"),
    "CWFLL":                 ("Chefs Warehouse, FL",    "Chefs Warehouse"),
    # Customers / additional distributors that surfaced in the historical
    # data and JD has tagged with a canonical mapping.
    # DeliBag is a South Korean account — full-container loads (1120 cs /
    # 20 pallets at a time) ship out under their own distributor entry.
    "DELIBAG":               ("DeliBag, South Korea",   "DeliBag"),
    "CARMELA FOODS":         ("Carmela Foods",          "Carmela Foods"),
    # Bare "CHENEY" without a city defaults to Riviera Beach per JD.
    "CHENEY":                ("Riviera Beach, FL",      "Cheney Brothers"),
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
    # leading digits before the warehouse name. Warehouse may contain
    # hyphens (e.g. "CW-MID-ATL", "CW-NY") — Chefs Warehouse routes use
    # them on the PDF header even though the subject uses other forms.
    r"(\d+)\s+Total\s+Cases\s*\d*\s*([A-Z][A-Z 0-9\-]*?)\s*\.\s*PO\s*\.\s*([A-Z0-9]+)",
    re.IGNORECASE,
)

# Fallback: extract the warehouse from a Daily Production subject line.
# Subjects observed in the wild:
#   "Daily Production OCALA.PO.054511694374"
#   "Daily Production US FOODS ZEBULON.PO.5210265G"
#   "Daily Production CW-MID-ATLANTIC PO.1095389"        (space, no dot)
#   "Daily production US FOODS MANASSAS PO.4363705O"     (lowercase 'p')
# Captures everything between "Daily Production " and the trailing
# "[. ]PO." token. The PDF header is still the source of truth when
# present; this fallback only fires when the body header didn't yield a
# warehouse.
_SUBJECT_WAREHOUSE_RE = re.compile(
    r"daily\s{1,3}production\s{1,3}(.{1,80}?)\s{0,3}[.\s]\s{0,3}PO\s{0,3}[.\s]\s{0,3}[A-Z0-9]+",
    re.IGNORECASE,
)

# Date inline in the lot column, e.g. "11890430264/30/2026" -> "4/30/2026"
_DATE_RE = re.compile(r"(?<!\d)((?:0?[1-9]|1[0-2])/\d{1,2}/\d{4})(?!\d)|(?:^|\D)((?:0?[1-9]|1[0-2])/\d{1,2}/\d{4})(?!\d)")


# --------------------------------------------------------------------------
# Warehouse classification
# --------------------------------------------------------------------------

# Aliases for partial "US FOODS <abbr>" — JD's production team uses
# short city abbreviations on the sheet that don't match the canonical
# city in _WAREHOUSE_TO_CANONICAL. Add new entries here as they surface.
_USF_CITY_ALIASES: dict[str, str] = {
    "LA": "LA MIRADA",     # observed in "US FOODS LA"
}


def _classify_warehouse(raw: str) -> "tuple[str, str]":
    """Resolve a raw warehouse label to (canonical_warehouse, distributor).

    Order of precedence:
      1. Empty / missing -> ("In-House Inventory", "H&H Bagels") —
         production sheets without a header pointed at a specific
         distributor PO; per JD these are baked for general inventory.
      2. Exact match in _WAREHOUSE_TO_CANONICAL
      3. "CW<XX>" 2-letter state code   -> Chefs Warehouse, <XX>
      4. "US FOODS <CITY-OR-ABBR>" prefix -> look up city (with alias
         table for shorthand like LA -> LA MIRADA)
      5. Otherwise, pass through the raw label with empty distributor

    Always returns a 2-tuple.
    """
    if not raw or not raw.strip():
        return "In-House Inventory", "H&H Bagels"
    key = raw.strip().upper()
    canonical = _WAREHOUSE_TO_CANONICAL.get(key)
    if canonical:
        return canonical

    # 2-letter CW<STATE> codes (CWDC, CWTX, etc.). Multi-letter region
    # codes (CWC, CWMID) are handled via explicit entries above.
    cw_match = re.match(r"^CW([A-Z]{2})$", key)
    if cw_match:
        return (f"Chefs Warehouse, {cw_match.group(1)}", "Chefs Warehouse")

    # "US FOODS <CITY>" — strip the prefix and look up the city alone.
    # Apply alias table (LA -> LA MIRADA) so shorthand variants resolve.
    if key.startswith("US FOODS "):
        city_key = key[len("US FOODS "):].strip()
        city_key = _USF_CITY_ALIASES.get(city_key, city_key)
        cm = _WAREHOUSE_TO_CANONICAL.get(city_key)
        if cm:
            return cm

    return (raw, "")


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
        sheet.warehouse, sheet.distributor = _classify_warehouse(sheet.warehouse_raw)

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
        except ValueError:
            # Only a malformed date string (bad split / int) should fall back to
            # the raw value; anything else is a real bug and should surface.
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
        canonical, recognized = _normalize_variety(raw_v)
        if not recognized:
            seen_unknown.add(raw_v)
        sheet.lines.append(ProductionLine(
            variety=canonical, raw_variety=raw_v, cs_count=cs,
        ))
    sheet.unmapped_varieties = sorted(seen_unknown)

    # Pair each line with its lot for traceability.
    # Lots are queued per variety (item-code -> variety via
    # HH_MFG_CODE_TO_VARIETY) and assigned to each matching line in
    # text-position order. This handles:
    #   - The single-day case (one lot per line, same variety)
    #   - Multi-day production (same variety appears on multiple lines
    #     with different lots — each line gets its own)
    #   - PDF quirks where one lot lands above the "Lot#" header in
    #     text-flow (we sort by source text offset, not column position)
    try:
        from .hh_mfg_codes import HH_MFG_CODE_TO_VARIETY
    except ImportError:  # standalone test invocation
        from hh_mfg_codes import HH_MFG_CODE_TO_VARIETY  # type: ignore
    lots_by_variety: dict = {}
    for lot in _extract_lots(text):
        canonical = HH_MFG_CODE_TO_VARIETY.get(lot["item_code"])
        if not canonical:
            continue
        lots_by_variety.setdefault(canonical, []).append(lot["lot"])
    for line in sheet.lines:
        queue = lots_by_variety.get(line.variety)
        if queue:
            line.lot_number = queue.pop(0)

    # If the header total didn't parse but we got lines, compute it.
    if not sheet.total_cases and sheet.lines:
        sheet.total_cases = sum(L.cs_count for L in sheet.lines)

    # Subject fallback — pull PO and/or warehouse out of the subject when
    # the PDF body header didn't yield them. Common cases: image-only
    # scan PDFs (no extractable text) and layouts where pypdf splits the
    # header line in a way the regex above misses.
    if not sheet.po_number and subject:
        sm = re.search(r"PO[._\s]\s*([A-Z0-9]+)", subject, re.IGNORECASE)
        if sm:
            sheet.po_number = sm.group(1).strip()
    if not sheet.warehouse_raw and subject:
        sm = _SUBJECT_WAREHOUSE_RE.search(subject)
        if sm:
            sheet.warehouse_raw = sm.group(1).strip()
            sheet.warehouse, sheet.distributor = _classify_warehouse(
                sheet.warehouse_raw,
            )

    if not sheet.lines:
        sheet.error = (sheet.error or "") + " no line items detected".strip()

    return sheet


_DIGIT_RUN_RE = re.compile(r"\d{10,}")


def _extract_lots(text: str) -> list:
    """Walk every digit run in the text and return the lots we can
    confidently parse.

    Each lot is {lot: str, item_code: str, date_iso: str}. Preserves
    order of appearance in the source text so callers that need to
    pair lots with line items positionally can do so.

    H&H lot codes are <4-digit item><MMDDYYYY> (current 12-digit) or
    <4-digit item><MMDDYY> (legacy 10-digit). We prefer the 12-digit
    interpretation per run and fall back to 10-digit when nothing
    plausible parsed at 12.
    """
    out = []
    seen_starts = set()
    for m in _DIGIT_RUN_RE.finditer(text):
        run = m.group(0)
        run_offset = m.start()
        run_hits: list = []
        # 12-digit pass
        i = 0
        while i <= len(run) - 12:
            window = run[i:i+12]
            mm, dd, yyyy = window[4:6], window[6:8], window[8:12]
            try:
                mm_i, dd_i, yyyy_i = int(mm), int(dd), int(yyyy)
            except ValueError:
                i += 1
                continue
            if (1 <= mm_i <= 12 and 1 <= dd_i <= 31
                    and 1900 <= yyyy_i <= 2099):
                run_hits.append({
                    "lot":       window,
                    "item_code": window[:4],
                    "date_iso":  f"{yyyy_i:04d}-{mm_i:02d}-{dd_i:02d}",
                    "_offset":   run_offset + i,
                })
                i += 12     # consume this window so it isn't re-matched
                continue
            i += 1
        # 10-digit fallback only if 12-digit found nothing
        if not run_hits:
            i = 0
            while i <= len(run) - 10:
                window = run[i:i+10]
                mm, dd, yy = window[4:6], window[6:8], window[8:10]
                try:
                    mm_i, dd_i, yy_i = int(mm), int(dd), int(yy)
                except ValueError:
                    i += 1
                    continue
                if 1 <= mm_i <= 12 and 1 <= dd_i <= 31:
                    year = 2000 + yy_i if yy_i < 70 else 1900 + yy_i
                    run_hits.append({
                        "lot":       window,
                        "item_code": window[:4],
                        "date_iso":  f"{year:04d}-{mm_i:02d}-{dd_i:02d}",
                        "_offset":   run_offset + i,
                    })
                    i += 10
                    continue
                i += 1
        out.extend(run_hits)
    # Final sort: by source text offset so positional pairing works
    out.sort(key=lambda x: x["_offset"])
    return out


def _dates_from_lot_numbers(text: str) -> set:
    """Convenience wrapper: return the distinct ISO dates encoded in lot
    numbers. Use _extract_lots when you need the full lot tuples
    (e.g. for per-line traceability)."""
    return {lot["date_iso"] for lot in _extract_lots(text)}


def _extract_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


__all__ = [
    "ProductionLine", "ProductionSheet",
    "parse_production_pdf", "parse_production_text",
]
