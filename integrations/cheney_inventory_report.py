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
from typing import Optional

try:  # package import
    from .hh_mfg_codes import HH_MFG_CODE_TO_VARIETY
    from .parsers._common import (canonical_variety, normalize_to_cases,
                                  opt_float, opt_int)
    from .parsers.inventory_csv import _build_name
except ImportError:  # pragma: no cover - standalone/test
    from hh_mfg_codes import HH_MFG_CODE_TO_VARIETY  # type: ignore
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


def _variety(mfg: str, desc: str) -> str:
    return (HH_MFG_CODE_TO_VARIETY.get(_clean_code(mfg))
            or canonical_variety(desc) or _desc_keyword(desc))


# --- header role detection (fuzzy; headers aren't contract-fixed) -----------
def _is_qty(h: str) -> bool:
    if "order" in h or "usage" in h or "weekly" in h:
        return False
    return ("on hand" in h or "on-hand" in h or "qoh" in h or "cs oh" in h
            or "cases on hand" in h or "cs_qty" in h or "cs qty" in h
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

    headers_seen: list[str] = []
    idx = 0
    try:
        for ws in wb.worksheets:
            rows = [[_cell_str(c) for c in row] for row in ws.iter_rows(values_only=True)]
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
                variety = _variety(mfg, desc)
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
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass

    if not events and not errors:
        seen = ("; ".join(headers_seen)) or "no non-empty rows"
        errors.append(f"{filename}: no recognizable inventory grid "
                      f"(warehouse={warehouse}). First rows seen: {seen}")
    return events, errors


__all__ = ["parse_report_xlsx", "warehouse_from_filename", "DISTRIBUTOR"]
