"""Parser for the US Foods "Weekly Bagel Inventory & Usage Report".

A US Foods warehouse rep (e.g. Zebulon, NC) emails a point-in-time inventory
and usage report *pasted directly into the message body* as an HTML table --
there is no attachment. JD standardized the columns with the rep so each row
carries the H&H MFG code and item name for a sanity check::

    ITEM     Vendor#  Description                CURRENT ON HAND  ON ORDER ETA 6/10  Forecast 5/31/2026  Forecast 6/7/2026 ...
    1055010  1184     BAGEL, EGG 4.06 Z UNSL...  23               16                 5.39                5.54              ...
    7095637  1150     BAGEL, PLN 4.25 Z UNSL...  81               96                 22.28               22.9              ...

Column meanings:
  - ``ITEM``            -- US Foods catalog item number (informational SKU).
  - ``Vendor#``         -- H&H's own internal MFG code (the same code USF and
                           Cheney print on POs -- see hh_mfg_codes). Variety is
                           resolved from this.
  - ``CURRENT ON HAND`` -- cases currently on hand at the rep's DC.
  - ``ON ORDER ...``    -- cases on an open PO (informational only; NOT applied
                           to inventory here -- the PO is ingested separately
                           from its own confirmation, so applying it again
                           would double count. The report body itself excludes
                           the open PO from CURRENT ON HAND).
  - ``Forecast <date>`` -- expected weekly usage (cases) for each of the next
                           ~13 weeks. We take the nearest week (the earliest-
                           dated forecast column) as the ``weekly_usage``
                           reference, mirroring the .xlsx worksheet parser.

Like the .xlsx worksheet, this report is a current-on-hand snapshot plus a
usage reference, so each mapped line becomes one ``on_hand`` event whose
``SyncItem`` also carries ``weekly_usage`` -- the apply path refreshes both the
on-hand quantity and the usage rate (sync_inventory._apply_email_event).

Two delivery shapes are understood, both handled here:

  - **Body table** (e.g. Zebulon, NC) -- the rep pastes the report into the
    message as an HTML table (``parse_report_html``; text/plain fallback
    ``parse_report_text``). ``Forecast <week>`` columns give the usage.
  - **.xlsx attachment** (e.g. Manassas, VA -- "Product Usage" report) -- the
    rep attaches a spreadsheet with ``Product Number`` / ``Cases`` (cases used
    in a one-week time frame) / ``Cases On Hand`` (``parse_report_xlsx``). This
    is a different layout from the CS OH / WKLY USE worksheet handled by
    ``bagel_inventory_worksheet`` and is self-detected by its header.

The report never names the warehouse; the *sending rep's email address*
identifies it (``REPORT_SENDER_TO_WAREHOUSE``). Add each rep here as they come
online -- an unknown sender is surfaced as an error rather than guessed.

No third-party HTML library is used: the table is extracted with the stdlib
``html.parser`` so the Render worker keeps its minimal dependency set.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from typing import Optional

try:
    from .hh_mfg_codes import HH_MFG_CODE_TO_VARIETY as _HH_MFG_MAP
except ImportError:  # standalone / test use
    from hh_mfg_codes import HH_MFG_CODE_TO_VARIETY as _HH_MFG_MAP  # type: ignore


# Report-sender email (lowercased) -> (distributor, canonical warehouse).
# The canonical warehouse string must match seed_bagels.py exactly. Extend as
# each US Foods DC rep starts sending the weekly body report.
REPORT_SENDER_TO_WAREHOUSE: dict[str, tuple[str, str]] = {
    "maria.hernandez@usfoods.com": ("US Foods", "Zebulon, NC"),
    "jasmin.gomez@usfoods.com": ("US Foods", "Manassas, VA"),
    # Manassas (DC 5O) street-sales shared mailbox, in case the report comes
    # from the team alias rather than a named coordinator.
    "5o-dl-streetsalescoordination@usfoods.com": ("US Foods", "Manassas, VA"),
}

# Fallback: US Foods catalog item # -> H&H MFG code. Only used when a report
# arrives WITHOUT the Vendor# column (the original format JD asked the rep to
# augment). Seeded from the first MFG-coded report; extend if USF adds SKUs.
USF_ITEM_NO_TO_MFG: dict[str, str] = {
    "1055010": "1184",  # Egg
    "1055061": "1171",  # Blueberry
    "1055064": "1159",  # Asiago
    "1055074": "1189",  # Jalapeno Cheddar
    "1137644": "1157",  # Whole Wheat Everything
    "1198923": "1156",  # Whole Wheat
    "1528283": "1152",  # Poppy Seed
    "2954526": "1155",  # Cinnamon Raisin
    "6950804": "1153",  # Sesame
    "7095637": "1150",  # Plain
    "7309056": "1158",  # Everything
    "7928199": "1151",  # Onion
}

# Cases per case is fixed at 60 (5 doz) -- same value seed_bagels.py stamps on
# every SKU and the .xlsx worksheet parser uses.
CASE_SIZE = 60


def warehouse_for_sender(from_header: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve a sender's From: header to (distributor, warehouse).

    Returns (None, None) if the sender is not a known report rep.
    """
    m = re.search(r"[\w.\-+]+@[\w.\-]+", from_header or "")
    if not m:
        return None, None
    return REPORT_SENDER_TO_WAREHOUSE.get(m.group(0).lower(), (None, None))


@dataclass
class ReportLine:
    """One bagel row from an inventory & usage report."""
    usf_item_no: str
    mfg_code: str
    description: str
    variety: Optional[str]          # canonical H&H variety (None = unmapped)
    cases_on_hand: float
    on_order: Optional[float]
    weekly_usage: Optional[float]


@dataclass
class InventoryReport:
    """A parsed inventory & usage report for one warehouse."""
    distributor: str = ""
    warehouse: str = ""
    week_label: str = ""            # the forecast column used, e.g. "5/31/2026"
    lines: list = field(default_factory=list)
    unmapped_codes: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Table extraction
# ---------------------------------------------------------------------------

class _TableExtractor(HTMLParser):
    """Collect every <table> in the document as a list of rows of cell text."""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: Optional[list] = None
        self._row: Optional[list] = None
        self._cell: Optional[list] = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _extract_tables_html(html: str) -> list[list[list[str]]]:
    p = _TableExtractor()
    try:
        p.feed(html or "")
    except Exception:  # noqa: BLE001 -- malformed HTML shouldn't crash a scan
        pass
    return p.tables


_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _norm(s: str) -> str:
    """Collapse a header cell to lowercase alphanumerics for matching."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    m = _NUM_RE.search(str(v).replace(",", ""))
    return float(m.group(0)) if m else None


def _clean_code(v) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _parse_header_date(label: str) -> Optional[date]:
    m = _DATE_RE.search(label or "")
    if not m:
        return None
    mo, dy, yr = (int(x) for x in m.groups())
    if yr < 100:
        yr += 2000
    try:
        return date(yr, mo, dy)
    except ValueError:
        return None


def _is_report_header(row: list[str]) -> bool:
    norms = [_norm(c) for c in row]
    has_item = any(n == "item" for n in norms)
    has_on_hand = any("onhand" in n for n in norms)
    return has_item and has_on_hand


def _build_report_from_table(table: list[list[str]],
                             distributor: str,
                             warehouse: str) -> Optional[InventoryReport]:
    """Turn one extracted <table> into an InventoryReport, or None if the
    table isn't an inventory & usage report."""
    if not table:
        return None
    # Find the header row (usually row 0, but tolerate a leading title row).
    header_idx = next((i for i, r in enumerate(table) if _is_report_header(r)),
                      None)
    if header_idx is None:
        return None

    header = table[header_idx]
    norms = [_norm(c) for c in header]

    col_item = col_mfg = col_desc = col_oh = col_oo = None
    week_cols: list[tuple[int, Optional[date]]] = []  # (col_idx, date|None)
    for idx, (raw, n) in enumerate(zip(header, norms)):
        if n == "item" and col_item is None:
            col_item = idx
        elif (n.startswith("vendor") or n.startswith("mfg")) and col_mfg is None:
            col_mfg = idx
        elif n == "description" and col_desc is None:
            col_desc = idx
        elif "onhand" in n and col_oh is None:
            col_oh = idx
        elif n.startswith("onorder") and col_oo is None:
            col_oo = idx
        elif n.startswith("forecast") or _DATE_RE.search(raw):
            week_cols.append((idx, _parse_header_date(raw)))

    if col_oh is None or (col_item is None and col_mfg is None):
        return None

    # Nearest week = earliest-dated forecast column (fall back to leftmost when
    # the dates don't parse). That is the current operating week's usage rate.
    week_col = None
    week_label = ""
    if week_cols:
        ordered = sorted(
            week_cols,
            key=lambda wc: (wc[1] is None, wc[1] or date.max, wc[0]),
        )
        week_col = ordered[0][0]
        week_label = header[week_col]

    report = InventoryReport(distributor=distributor, warehouse=warehouse,
                             week_label=week_label)

    for r in table[header_idx + 1:]:
        usf_item = _clean_code(r[col_item]) if (col_item is not None and col_item < len(r)) else ""
        mfg = _clean_code(r[col_mfg]) if (col_mfg is not None and col_mfg < len(r)) else ""
        # Qualify a data row: it must carry a numeric item or mfg code AND a
        # numeric on-hand value. Skips header repeats, blank, and total rows.
        if not (re.fullmatch(r"\d{3,}", usf_item) or re.fullmatch(r"\d{3,}", mfg)):
            continue
        on_hand = _to_float(r[col_oh]) if col_oh < len(r) else None
        if on_hand is None:
            continue
        on_order = _to_float(r[col_oo]) if (col_oo is not None and col_oo < len(r)) else None
        weekly = _to_float(r[week_col]) if (week_col is not None and week_col < len(r)) else None
        desc = r[col_desc].strip() if (col_desc is not None and col_desc < len(r)) else ""

        # Variety via MFG code, falling back to USF item # -> MFG.
        code = mfg or USF_ITEM_NO_TO_MFG.get(usf_item, "")
        variety = _HH_MFG_MAP.get(code)
        if variety is None:
            report.unmapped_codes.append(mfg or usf_item)

        report.lines.append(ReportLine(
            usf_item_no=usf_item,
            mfg_code=code,
            description=desc,
            variety=variety,
            cases_on_hand=on_hand,
            on_order=on_order,
            weekly_usage=weekly,
        ))

    return report if report.lines else None


def looks_like_report(subject: str = "", body: str = "") -> bool:
    """Heuristic: does this message look like an inventory & usage report?

    Used to decide whether to surface an "unknown rep" error for a report from
    a sender that isn't yet mapped -- so a new DC rep's first report doesn't get
    silently dropped, while unrelated mail stays quiet.
    """
    s = (subject or "").lower()
    if "inventory" in s and ("usage" in s or "weekly" in s):
        return True
    b = (body or "").lower()
    return "current on hand" in b


def parse_report_html(html: str, *, distributor: str = "",
                      warehouse: str = "") -> Optional[InventoryReport]:
    """Parse the FIRST inventory & usage report table in an HTML body.

    The latest reply's table comes first in the HTML, so quoted older reports
    further down the thread are ignored. Returns None if no report table is
    found.
    """
    for table in _extract_tables_html(html):
        report = _build_report_from_table(table, distributor, warehouse)
        if report is not None:
            return report
    return None


def parse_report_text(text: str, *, distributor: str = "",
                      warehouse: str = "") -> Optional[InventoryReport]:
    """Fallback parser for a plain-text body (no HTML table).

    Outlook flattens the table to one cell per line separated by blank lines.
    We rebuild rows by finding the ITEM header, treating every non-empty line
    up to the first all-digit item number as the header, then chunking the
    remaining cells into rows of that width.
    """
    if not text:
        return None
    cells = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Locate the header start ("ITEM") that is followed (somewhere) by a
    # "CURRENT ON HAND" cell.
    start = None
    for i, c in enumerate(cells):
        if _norm(c) == "item":
            window = cells[i:i + 25]
            if any("onhand" in _norm(w) for w in window):
                start = i
                break
    if start is None:
        return None
    # Header runs until the first cell that is a bare item number (>=4 digits).
    j = start + 1
    while j < len(cells) and not re.fullmatch(r"\d{4,}", cells[j]):
        j += 1
    header = cells[start:j]
    ncols = len(header)
    if ncols < 3:
        return None
    body_cells = cells[j:]
    # Chunk into rows; stop if a chunk doesn't start with a numeric item code
    # (signals we've run past the table into signature/quoted text).
    rows = [header]
    for k in range(0, len(body_cells) - ncols + 1, ncols):
        chunk = body_cells[k:k + ncols]
        if not re.fullmatch(r"\d{3,}", chunk[0]):
            break
        rows.append(chunk)
    return _build_report_from_table(rows, distributor, warehouse)


def parse_report_xlsx(xlsx_bytes: bytes, *, distributor: str = "",
                      warehouse: str = "") -> Optional[InventoryReport]:
    """Parse a US Foods "Product Usage" .xlsx report into an InventoryReport.

    Sheet layout (one sheet, e.g. "Product Usage")::

        Time Frame May 24 - 31 | Customer Name | Customer Number | Product Number | Product Description | Cases |  | Cases On Hand
        Total Time Frame       | HH BAGELS     | 91634139        | 1055064        | BAGEL, ASIGO ...    | 3     |  | 22

    ``Cases`` is the cases consumed during the (one-week) time frame, taken as
    the ``weekly_usage`` reference. ``Cases On Hand`` is the current snapshot.
    The ``Product Number`` is the USF catalog item # -- there is no Vendor#/MFG
    column -- so variety resolves through the ``USF_ITEM_NO_TO_MFG`` fallback.

    Returns None when the workbook isn't a recognizable Product Usage report,
    so the caller can fall through to other parsers without raising.
    """
    try:
        import openpyxl  # local import; openpyxl is already a project dep
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True,
                                    read_only=True)
    except Exception:  # noqa: BLE001 -- a non-xlsx / corrupt file isn't a report
        return None

    rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
    if not rows:
        return None

    _ITEM_HEADERS = ("productnumber", "productnum", "productno", "item")

    header_idx = None
    for i, row in enumerate(rows):
        norms = [_norm(c) for c in row if c is not None]
        if any(n in _ITEM_HEADERS for n in norms) and any("onhand" in n for n in norms):
            header_idx = i
            break
    if header_idx is None:
        return None

    header = rows[header_idx]
    norms = [_norm(c) for c in header]
    col_item = col_mfg = col_desc = col_oh = col_use = None
    week_label = ""
    for idx, (raw, n) in enumerate(zip(header, norms)):
        if n in _ITEM_HEADERS and col_item is None:
            col_item = idx
        elif (n.startswith("vendor") or n.startswith("mfg")) and col_mfg is None:
            col_mfg = idx
        elif "description" in n and col_desc is None:
            col_desc = idx
        elif "onhand" in n and col_oh is None:
            col_oh = idx
        elif n == "cases" and col_use is None:   # exact: not "casesonhand"
            col_use = idx
        if raw and "time frame" in str(raw).lower() and not week_label:
            week_label = str(raw).strip()

    if col_oh is None or col_item is None:
        return None

    report = InventoryReport(distributor=distributor, warehouse=warehouse,
                             week_label=week_label)
    for r in rows[header_idx + 1:]:
        usf_item = _clean_code(r[col_item]) if col_item < len(r) else ""
        if not re.fullmatch(r"\d{3,}", usf_item):
            continue
        on_hand = _to_float(r[col_oh]) if col_oh < len(r) else None
        if on_hand is None:
            continue
        weekly = _to_float(r[col_use]) if (col_use is not None and col_use < len(r)) else None
        mfg = _clean_code(r[col_mfg]) if (col_mfg is not None and col_mfg < len(r)) else ""
        desc = (str(r[col_desc]).strip()
                if (col_desc is not None and col_desc < len(r) and r[col_desc] is not None)
                else "")
        code = mfg or USF_ITEM_NO_TO_MFG.get(usf_item, "")
        variety = _HH_MFG_MAP.get(code)
        if variety is None:
            report.unmapped_codes.append(mfg or usf_item)
        report.lines.append(ReportLine(
            usf_item_no=usf_item,
            mfg_code=code,
            description=desc,
            variety=variety,
            cases_on_hand=on_hand,
            on_order=None,
            weekly_usage=weekly,
        ))

    return report if report.lines else None


__all__ = [
    "REPORT_SENDER_TO_WAREHOUSE",
    "USF_ITEM_NO_TO_MFG",
    "CASE_SIZE",
    "warehouse_for_sender",
    "ReportLine",
    "InventoryReport",
    "looks_like_report",
    "parse_report_html",
    "parse_report_text",
    "parse_report_xlsx",
]
