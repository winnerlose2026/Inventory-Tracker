"""Parser for distributor "Bagel Inventory and Weekly Usage" worksheets.

A warehouse rep emails an ``.xlsx`` worksheet that lists, per bagel variety,
the current cases on hand (``CS OH``) and the average weekly usage
(``WKLY USE``) at their distribution center. Unlike a PO -- which announces a
restock -- this is a point-in-time on-hand snapshot plus a usage reference, so
the email scanner turns each line into an ``on_hand`` event whose ``SyncItem``
also carries ``weekly_usage``.

Worksheet layout (one sheet, e.g. "H&H Bagel Inventory")::

    DATE   |   |            | 6.1.26 |        | 6.8.26 |        | ...
    USF #  |MFG#|DESCRIPTION | CS OH  |WKLY USE| CS OH  |WKLY USE| ...
    1055064|1159| BAGEL, ...|   48   |   6    |        |        |
    ...

The rep adds one ``CS OH`` / ``WKLY USE`` column-pair per week and fills the
column for the current week. We read the LATEST-dated pair that actually has
data, so a sheet that accumulates weeks and a sheet that carries a single week
both resolve to "this week's snapshot".

The worksheet never names the warehouse -- the *sending rep's email address*
identifies it. ``REP_EMAIL_TO_WAREHOUSE`` maps each known rep to their
``(distributor, canonical warehouse)``. Add new reps here as each warehouse
rep comes online; an unknown sender is surfaced as an error rather than
guessed.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

try:
    from .hh_mfg_codes import HH_MFG_CODE_TO_VARIETY as _HH_MFG_MAP
except ImportError:  # standalone / test use
    from hh_mfg_codes import HH_MFG_CODE_TO_VARIETY as _HH_MFG_MAP  # type: ignore


# Rep email address (lowercased) -> (distributor, canonical warehouse).
# The canonical warehouse string must match seed_bagels.py exactly so the
# reconstructed SKU name ("<Variety> Bagel 4oz [USF - Chicago]") matches the
# seeded inventory key. Extend as each warehouse rep starts sending weekly
# inventory worksheets.
REP_EMAIL_TO_WAREHOUSE: dict[str, tuple[str, str]] = {
    "michael.via@usfoods.com": ("US Foods", "Chicago, IL"),
}

# Cases per case is fixed at 60 (5 doz) across both distributors -- same value
# seed_bagels.py stamps on every SKU.
CASE_SIZE = 60


def warehouse_for_sender(from_header: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve a sender's From: header to (distributor, warehouse).

    Returns (None, None) if the sender is not a known inventory-worksheet rep.
    """
    m = re.search(r"[\w.\-+]+@[\w.\-]+", from_header or "")
    if not m:
        return None, None
    email_addr = m.group(0).lower()
    from integrations.rep_map import sender_overrides
    ov = sender_overrides()
    if email_addr in ov:
        return ov[email_addr]
    return REP_EMAIL_TO_WAREHOUSE.get(email_addr, (None, None))


@dataclass
class WorksheetLine:
    """One bagel row from an inventory worksheet."""
    mfg_code: str
    usf_item_no: str
    description: str
    variety: Optional[str]          # canonical H&H variety (None = unmapped)
    cases_on_hand: float
    weekly_usage: Optional[float]


@dataclass
class InventoryWorksheet:
    """A parsed inventory-and-usage worksheet for one warehouse."""
    distributor: str = ""
    warehouse: str = ""
    snapshot_label: str = ""        # the column's date label, e.g. "6.1.26"
    lines: list = field(default_factory=list)
    unmapped_codes: list = field(default_factory=list)


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM_RE.search(str(v).replace(",", ""))
    return float(m.group(0)) if m else None


def _parse_date_label(label) -> Optional[date]:
    """Parse a 'M.D.YY' / 'M/D/YYYY' style date label into a date for ordering.

    Returns None when the label can't be parsed; callers fall back to column
    position so an unparseable header never drops data.
    """
    if label is None:
        return None
    if hasattr(label, "year") and hasattr(label, "month"):  # datetime/date
        try:
            return date(label.year, label.month, label.day)
        except (TypeError, ValueError):
            return None
    parts = re.split(r"[./\-]", str(label).strip())
    if len(parts) < 3:
        return None
    try:
        mo, dy, yr = (int(p) for p in parts[:3])
    except ValueError:
        return None
    if yr < 100:
        yr += 2000
    try:
        return date(yr, mo, dy)
    except ValueError:
        return None


def _norm(v) -> str:
    return str(v).strip().lower() if v is not None else ""


def parse_worksheet_xlsx(xlsx_bytes: bytes, *, distributor: str,
                         warehouse: str) -> InventoryWorksheet:
    """Parse an inventory-worksheet .xlsx into an InventoryWorksheet.

    ``distributor`` and ``warehouse`` are resolved by the caller from the
    sending rep (see ``warehouse_for_sender``) because the file itself does
    not carry them.
    """
    import openpyxl  # local import; openpyxl is already a project dep

    ws_out = InventoryWorksheet(distributor=distributor, warehouse=warehouse)
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
    sheet = wb.active

    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    if not rows:
        return ws_out

    # Locate the header row (the one naming USF #, MFG #, DESCRIPTION) and the
    # DATE row that sits directly above it.
    header_idx = None
    for i, row in enumerate(rows):
        norm = [_norm(c) for c in row]
        if "mfg #" in norm and "description" in norm and "cs oh" in norm:
            header_idx = i
            break
    if header_idx is None:
        return ws_out

    header = [_norm(c) for c in rows[header_idx]]
    date_row = rows[header_idx - 1] if header_idx > 0 else []

    col_mfg = header.index("mfg #")
    col_desc = header.index("description")
    col_usf = header.index("usf #") if "usf #" in header else None

    # Build the (CS OH, WKLY USE) column pairs, with each pair's date label.
    pairs = []  # (date_or_None, position_idx, cs_col, wk_col, label)
    for c, name in enumerate(header):
        if name == "cs oh":
            wk_col = c + 1 if (c + 1) < len(header) and header[c + 1] == "wkly use" else None
            label = ""
            if c < len(date_row) and date_row[c] is not None:
                label = str(date_row[c]).strip()
            pairs.append((_parse_date_label(date_row[c] if c < len(date_row) else None),
                          c, c, wk_col, label))

    if not pairs:
        return ws_out

    data_rows = rows[header_idx + 1:]

    def _pair_has_data(cs_col: int) -> bool:
        for r in data_rows:
            if cs_col < len(r) and _to_float(r[cs_col]) is not None:
                return True
        return False

    # Choose the active pair: the latest-dated pair that actually has CS OH
    # data. Fall back to column order when dates are missing/unparseable.
    candidates = [p for p in pairs if _pair_has_data(p[2])]
    if not candidates:
        candidates = pairs  # nothing populated yet; still report the lines
    candidates.sort(key=lambda p: (p[0] is not None, p[0] or date.min, p[1]))
    active = candidates[-1]
    _, _, cs_col, wk_col, label = active
    ws_out.snapshot_label = label

    for r in data_rows:
        mfg_raw = r[col_mfg] if col_mfg < len(r) else None
        if mfg_raw is None or str(mfg_raw).strip() == "":
            continue
        # MFG codes are integers in the sheet (e.g. 1159) -> normalize to str.
        mfg_code = str(mfg_raw).strip()
        if mfg_code.endswith(".0"):
            mfg_code = mfg_code[:-2]
        cs_oh = _to_float(r[cs_col]) if cs_col < len(r) else None
        if cs_oh is None:
            continue
        wkly = _to_float(r[wk_col]) if (wk_col is not None and wk_col < len(r)) else None
        usf_no = ""
        if col_usf is not None and col_usf < len(r) and r[col_usf] is not None:
            usf_no = str(r[col_usf]).strip()
            if usf_no.endswith(".0"):
                usf_no = usf_no[:-2]
        desc = str(r[col_desc]).strip() if col_desc < len(r) and r[col_desc] is not None else ""
        variety = _HH_MFG_MAP.get(mfg_code)
        if variety is None:
            ws_out.unmapped_codes.append(mfg_code)
        ws_out.lines.append(WorksheetLine(
            mfg_code=mfg_code,
            usf_item_no=usf_no,
            description=desc,
            variety=variety,
            cases_on_hand=cs_oh,
            weekly_usage=wkly,
        ))

    return ws_out


__all__ = [
    "REP_EMAIL_TO_WAREHOUSE",
    "CASE_SIZE",
    "warehouse_for_sender",
    "WorksheetLine",
    "InventoryWorksheet",
    "parse_worksheet_xlsx",
]
