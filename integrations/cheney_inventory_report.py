"""Parser for Michael Ross's per-facility Cheney Brothers inventory & usage
spreadsheets — one ``.xlsx`` per Florida warehouse (e.g.
``H&HRVBMay272026.xlsx``, ``H&HOcalaMay272026.xlsx``,
``H&HPuntaGordaMay272026.xlsx``).

The warehouse is identified by the FILENAME (RVB/Riviera Beach, Ocala, Punta
Gorda), not by the sender (always Ross) or anything inside the sheet. Each data
row becomes an ``on_hand`` EmailEvent carrying ``weekly_usage`` for the
(variety x warehouse) SKU — the exact payload shape the SFTP inventory-CSV path
emits, so it flows through ``/api/email/ingest-events`` unchanged.

Variety resolution order: H&H Mfg# column (the same internal code Cheney prints
on POs — see ``hh_mfg_codes``), then the shared ``canonical_variety`` on the
description, then a Cheney-description keyword fallback. Header columns are
matched fuzzily because Ross's export headers aren't contract-fixed; if a sheet
yields nothing recognizable, the parser returns an explanatory error (with the
headers it saw) rather than silently ingesting nothing.
"""
from __future__ import annotations

import io
import re
from datetime import date
from typing import Optional

try:  # package import
    from .hh_mfg_codes import HH_MFG_CODE_TO_VARIETY, CHENEY_ITEM_NO_TO_MFG
    from .parsers._common import (canonical_variety, normalize_to_cases,
                                  opt_float, opt_int)
    from .parsers.inventory_csv import _build_name
except ImportError:  # pragma: no cover - standalone/test
    from hh_mfg_codes import HH_MFG_CODE_TO_VARIETY, CHENEY_ITEM_NO_TO_MFG  # type: ignore
    from parsers._common import (canonical_variety, normalize_to_cases,  # type: ignore
                                 opt_float, opt_int)
    from parsers.inventory_csv import _build_name  # type: ignore

DISTRIBUTOR = "Cheney Brothers"
DEFAULT_CASE_SIZE = 60

# Cheney-description keyword fallback (compound varieties first so
# "WHOLE WHEAT EVERYTHING" doesn't get tagged "Everything"). Used only when
# the H&H Mfg# is absent and canonical_variety() can't resolve the text.
_DESC_KEYWORDS = [
    ("whole wheat everything", "Whole Wheat Everything"),
    ("ww everything", "Whole Wheat Everything"),
    ("evthg whl wheat", "Whole Wheat Everything"),
    ("whole wheat", "Whole Wheat"),
    ("whl wheat", "Whole Wheat"),
    ("cinnamon raisin", "Cinnamon Raisin"),
    ("cinnamon", "Cinnamon Raisin"),
    ("cin rais", "Cinnamon Raisin"),
    ("raisin", "Cinnamon Raisin"),
    ("jalapeno", "Jalapeno Cheddar"),
    ("jalap", "Jalapeno Cheddar"),
    ("jlp", "Jalapeno Cheddar"),
    ("asiago", "Asiago"),
    ("asigo", "Asiago"),
    ("blueberry", "Blueberry"),
    ("poppy", "Poppy Seed"),
    ("sesame", "Sesame"),
    ("onion", "Onion"),
    ("everything", "Everything"),
    ("pumpernickel", "Pumpernickel"),
    ("egg", "Egg"),
    ("plain", "Plain"),
]


def warehouse_from_filename(name: str) -> str:
    """Map a Cheney per-facility filename to our canonical 'City, FL' label."""
    s = (name or "").lower()
    if "rvb" in s or "riviera" in s:
        return "Riviera Beach, FL"
    if "ocala" in s:
        return "Ocala, FL"
    if "puntagorda" in s or "punta gorda" in s or "punta" in s or "pgd" in s:
        return "Punta Gorda, FL"
    return ""


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return str(v).strip()


def _clean_code(v: str) -> str:
    s = (v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s.replace(" ", "")


def _desc_keyword(desc: str) -> str:
    s = (desc or "").lower()
    for sub, var in _DESC_KEYWORDS:
        if sub in s:
            return var
    return ""


def _variety(mfg: str, desc: str, sku: str = "") -> str:
    # Cheney's on-hand stock export has no Mfg# column; fall back to the
    # Cheney catalog # -> H&H mfg crosswalk before description heuristics.
    via_sku = HH_MFG_CODE_TO_VARIETY.get(
        CHENEY_ITEM_NO_TO_MFG.get(_clean_code(sku), ""))
    return (HH_MFG_CODE_TO_VARIETY.get(_clean_code(mfg))
            or via_sku
            or canonical_variety(desc) or _desc_keyword(desc))


# --- header role detection (fuzzy; headers aren't contract-fixed) -----------
def _is_qty(h: str) -> bool:
    if "order" in h or "usage" in h or "weekly" in h:
        return False
    return ("on hand" in h or "on-hand" in h or "qoh" in h or "cs oh" in h
            or "cases on hand" in h or "cs_qty" in h or "cs qty" in h
            or h == "stock"
            or h in ("quantity", "qty", "cases", "case qty", "on hand quantity",
                     "qty on hand", "oh", "current on hand"))


def _is_wu(h: str) -> bool:
    return ("usage" in h
            or ("weekly" in h and ("avg" in h or "average" in h
                                   or "demand" in h or "use" in h))
            or h in ("wkly use", "weekly usage", "avg weekly",
                     "weekly average demand", "weekly avg"))


def _is_mfg(h: str) -> bool:
    return ("mfg" in h
            or ("manufacturer" in h and ("number" in h or "product" in h or "#" in h)))


def _is_var(h: str) -> bool:
    return ("description" in h or "variety" in h
            or h in ("product", "product description", "item description", "item name"))


def _is_cs(h: str) -> bool:
    return ("case size" in h or "pack size" in h or "units per case" in h
            or h == "case_size")


def _is_sku(h: str) -> bool:
    return (("cheney" in h and "item" in h)
            or h in ("item #", "item number", "item no", "catalog", "product #", "sku"))


def _find_header(rows: list) -> Optional[tuple]:
    """Return (header_row_index, roles) for the first row that looks like a
    data-grid header (a quantity column + a variety-or-Mfg column)."""
    for i, row in enumerate(rows[:40]):
        hnorms = [(_cell_str(c) or "").lower() for c in row]
        roles: dict = {}
        for j, h in enumerate(hnorms):
            if not h:
                continue
            if "quantity" not in roles and _is_qty(h):
                roles["quantity"] = j
                roles["_qtyhdr"] = h
            if "weekly_usage" not in roles and _is_wu(h):
                roles["weekly_usage"] = j
            if "mfg" not in roles and _is_mfg(h):
                roles["mfg"] = j
            if "variety" not in roles and _is_var(h):
                roles["variety"] = j
            if "case_size" not in roles and _is_cs(h):
                roles["case_size"] = j
            if "distributor_sku" not in roles and _is_sku(h):
                roles["distributor_sku"] = j
        if "quantity" in roles and ("variety" in roles or "mfg" in roles):
            return i, roles
    return None


_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")


def _span_days(rows: list) -> tuple:
    """Find the 'Date Range >= lo AND <= hi' line; return
    (span_days, lo, hi, note). span_days counts both endpoints; defaults to 30
    when no usable range is present."""
    for r in rows:
        for c in r:
            if c and "date range" in c.lower():
                found = _DATE_RE.findall(c)
                if len(found) >= 2:
                    try:
                        mo, dy, yr = (int(x) for x in found[0].split("/"))
                        lo = date(yr, mo, dy)
                        mo, dy, yr = (int(x) for x in found[1].split("/"))
                        hi = date(yr, mo, dy)
                        span = (hi - lo).days + 1
                        if span > 0:
                            return span, found[0], found[1], ""
                    except ValueError:
                        pass
    return 30, "", "", "no usable date range found; defaulted span to 30 days"


def _find_cm_header(rows: list):
    """Locate a case-movement header: a 'Full Cases' column plus a
    'Dist Item #' or 'Mfq.Product Code' column. Returns
    (header_idx, col_cases, col_mfg, col_item) or None."""
    for i, row in enumerate(rows[:40]):
        h = [(_cell_str(c) or "").lower() for c in row]
        col_cases = next((k for k, x in enumerate(h) if "full cases" in x), None)
        if col_cases is None:
            continue
        col_mfg = next((k for k, x in enumerate(h)
                        if x.startswith("mfq") or x.startswith("mfg")), None)
        col_item = next((k for k, x in enumerate(h)
                         if "dist item" in x or x in ("item #", "item number")), None)
        if col_mfg is not None or col_item is not None:
            return i, col_cases, col_mfg, col_item
    return None


def _parse_case_movement(rows: list, warehouse: str, filename: str,
                         distributor: str) -> "tuple[list[dict], list[str]]":
    """Parse a Cheney case-movement (usage) sheet into usage_rate events.

    'Full Cases' totals the report's date range (currently a month); it is
    converted to a weekly average (total * 7 / span_days). These events carry
    weekly_usage ONLY -- the apply path refreshes the usage reference without
    touching cases on hand or writing a movement ledger entry. Returns
    ([], []) when the sheet isn't a case-movement export, so the caller falls
    through to the on-hand grid parser."""
    hdr = _find_cm_header(rows)
    if not hdr:
        return [], []
    hi, col_cases, col_mfg, col_item = hdr
    span, _lo, _hi, note = _span_days(rows)
    events: list[dict] = []
    errors: list[str] = []
    if note:
        errors.append(f"{warehouse}: {note}")
    for r in rows[hi + 1:]:
        mfg = _clean_code(r[col_mfg]) if (col_mfg is not None and col_mfg < len(r)) else ""
        sku = _clean_code(r[col_item]) if (col_item is not None and col_item < len(r)) else ""
        cases = opt_float(r[col_cases]) if col_cases < len(r) else None
        if cases is None:
            continue
        variety = (HH_MFG_CODE_TO_VARIETY.get(mfg)
                   or HH_MFG_CODE_TO_VARIETY.get(CHENEY_ITEM_NO_TO_MFG.get(sku, "")))
        if not variety:
            # Total rows (e.g. "Sum of All Products Activity") carry no code --
            # skip silently; surface only rows that DO carry an unmapped code.
            if mfg or sku:
                errors.append(f"{warehouse}: unmapped case-movement row "
                              f"(mfg={mfg!r}, item={sku!r})")
            continue
        weekly = round(cases * 7.0 / span, 2)
        events.append({
            "event_type": "usage_rate",
            "item": {
                "quantity": 0.0,
                "distributor": distributor,
                "name": _build_name(distributor, variety, warehouse),
                "variety": variety,
                "warehouse": warehouse,
                "unit": "cs",
                "weekly_usage": weekly,
            },
            "source_message_id": f"cheney-xlsx:{filename}",
            "source_subject": f"Cheney case movement: {filename}",
            "po_number": "",
            "po_revision": "",
        })
    return events, errors


def parse_report_xlsx(xlsx_bytes: bytes, filename: str, *,
                      distributor: str = DISTRIBUTOR) -> "tuple[list[dict], list[str]]":
    events: list[dict] = []
    errors: list[str] = []
    warehouse = warehouse_from_filename(filename)
    if not warehouse:
        errors.append(f"could not determine Cheney warehouse from filename {filename!r}")
        return events, errors
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{filename}: cannot open xlsx: {type(exc).__name__}: {exc}")
        return events, errors
    try:
        rows_by_sheet = [
            [[_cell_str(c) for c in row] for row in ws.iter_rows(values_only=True)]
            for ws in wb.worksheets
        ]
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass

    # Case-movement (usage) export -> usage_rate events. Ross sends this
    # SEPARATELY from the on-hand stock sheet; it has a "Full Cases" total over
    # a date range and NO cases-on-hand, so it must not be read as on_hand.
    for rows in rows_by_sheet:
        cm_events, cm_errors = _parse_case_movement(rows, warehouse, filename, distributor)
        if cm_events or cm_errors:
            return cm_events, cm_errors

    # On-hand stock grid.
    headers_seen: list[str] = []
    idx = 0
    for rows in rows_by_sheet:
        hdr = _find_header(rows)
        if not hdr:
            for r in rows[:6]:
                if any(r):
                    headers_seen.append(" | ".join(x for x in r if x)[:120])
                    break
            continue
        hi, roles = hdr
        qhdr = roles.get("_qtyhdr", "")
        unit_raw = "each" if any(t in qhdr for t in ("each", "eaches", " ea", "unit")) else "cs"

        def cell(role: str) -> str:
            j = roles.get(role)
            return r[j] if (j is not None and j < len(r)) else ""

        blanks = 0
        for r in rows[hi + 1:]:
            if not any(r):
                blanks += 1
                if blanks >= 5:
                    break
                continue
            blanks = 0
            mfg = cell("mfg")
            desc = cell("variety")
            variety = _variety(mfg, desc, cell("distributor_sku"))
            qty = opt_float(cell("quantity"))
            if not variety:
                if qty is not None or desc or _clean_code(mfg):
                    errors.append(f"{warehouse}: unmapped row "
                                  f"(mfg={_clean_code(mfg)!r}, desc={desc!r})")
                continue
            if qty is None:
                continue
            cs = opt_int(cell("case_size")) or DEFAULT_CASE_SIZE
            qty_norm, unit_norm = normalize_to_cases(qty, unit_raw, cs)
            idx += 1
            item: dict = {
                "quantity": qty_norm,
                "distributor": distributor,
                "name": _build_name(distributor, variety, warehouse),
                "variety": variety,
                "warehouse": warehouse,
                "unit": unit_norm,
                "case_size": cs,
            }
            wu = opt_float(cell("weekly_usage"))
            if wu is not None:
                item["weekly_usage"] = wu
            sku = cell("distributor_sku")
            if sku:
                item["distributor_sku"] = sku
            events.append({
                "event_type": "on_hand",
                "item": item,
                "source_message_id": f"cheney-xlsx:{filename}#{idx}",
                "source_subject": f"Cheney inventory & usage: {filename}",
                "po_number": "",
                "po_revision": "",
            })

    if not events and not errors:
        seen = ("; ".join(headers_seen)) or "no non-empty rows"
        errors.append(f"{filename}: no recognizable inventory grid "
                      f"(warehouse={warehouse}). First rows seen: {seen}")
    return events, errors


__all__ = ["parse_report_xlsx", "warehouse_from_filename", "DISTRIBUTOR"]
