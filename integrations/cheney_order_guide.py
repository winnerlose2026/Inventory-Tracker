"""Parser for Cheney Brothers' daily "OrderGuide" CSV (SFTP feed).

WHAT THIS FILE IS -- AND IS NOT
------------------------------
On 2026-08-04 Jairo Henao (Cheney software dev director) sent the first real
sample of the daily CSV as ``HHBGELES_OrderGuide_sample.zip``. It is an **order
guide**: the catalog of items each H&H account may order from its serving DC,
with pack size and current case cost. It is **not** the per-DC on-hand
inventory snapshot we asked Walt for on 2026-07-06.

Specifically, across all 9 sample files / 1,882 rows:

    * the on-hand column is present but is ``0`` on **every single row**;
    * there is no product description -- only an 8-character brand
      abbreviation ("KRAFT", "SCHREIBE", "OPENMEADOW");
    * files are one-per-**store account**, not one-per-DC;
    * there is no header row at all.

So this parser deliberately does NOT produce ``on_hand`` events. Feeding an
all-zero quantity column into the on-hand path would write 0 cases for every
item at every warehouse and erase the tracker's real counts.
``cheney_csv_inventory.parse_inventory_csv`` refuses these files for that
reason; see ``looks_like_order_guide``.

LAYOUT (headerless, 7 columns, verified against all 9 sample files)
------------------------------------------------------------------
    0  Cheney item #                     "10064422"
    1  brand / label abbreviation        "SCHREIBE"      (matches 810 BL)
    2  DC code, last 2 digits            "05"  -> 3005 Ocala
    3  on-hand cases                     always "0" in the 2026-08-04 drop
    4  pack / size                       "004 5LB"       (matches 810 PO4)
    5  case cost                         "4.17"
    6  snapshot timestamp                "20260803113325"

Filename: ``OrderGuide-<YYYYMMDD>-<HHMMSS><seq>_<account>.csv``, where
``account`` is the Cheney ship-to account # (see ``cheney_dcs``).

Note a same item # can legitimately appear twice in one file with two
different case costs (e.g. 140056 FRONTE at 10.21 and 47.07) -- Cheney prices
some items at more than one pack/split. Rows are returned as-is; dedup is the
caller's decision.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

try:  # package import
    from .cheney_dcs import (
        warehouse_from_dc_code, dc_name, is_known_dc, normalize_dc_code,
        store_from_account,
    )
    from .cheney_inventory_report import (
        _variety, _clean_code, _build_name, DEFAULT_CASE_SIZE)
    from .cheney_po_parser import CHENEY_CASE_COST
    from .parsers._common import opt_float
except ImportError:  # standalone (tests / CLI)
    from cheney_dcs import (  # type: ignore
        warehouse_from_dc_code, dc_name, is_known_dc, normalize_dc_code,
        store_from_account,
    )
    from cheney_inventory_report import (  # type: ignore
        _variety, _clean_code, _build_name, DEFAULT_CASE_SIZE)
    from cheney_po_parser import CHENEY_CASE_COST  # type: ignore
    from parsers._common import opt_float  # type: ignore

DISTRIBUTOR = "Cheney Brothers"

_FILENAME_RE = re.compile(
    r"OrderGuide[-_](?P<date>\d{8})[-_](?P<time>\d{6})\d*_(?P<account>\d+)",
    re.IGNORECASE,
)

# "001 30LB" / "024 12OZ" / "001 60CT" -> (pack count, size text)
_PACK_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")


def account_from_filename(filename: str) -> str:
    m = _FILENAME_RE.search(filename or "")
    return (m.group("account").lstrip("0") if m else "")


def snapshot_from_filename(filename: str) -> str:
    """Order-guide filename -> ISO 'YYYY-MM-DD', or ''."""
    m = _FILENAME_RE.search(filename or "")
    if not m:
        return ""
    try:
        return datetime.strptime(m.group("date"), "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _iso_from_stamp(v: str) -> str:
    """'20260803113325' -> '2026-08-03'. Empty when unparseable."""
    s = re.sub(r"\D", "", v or "")
    if len(s) < 8:
        return ""
    try:
        return datetime.strptime(s[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _split_pack(v: str):
    """'004 5LB' -> (4, '5LB'). Returns (None, raw) when it doesn't match."""
    m = _PACK_RE.match(v or "")
    if not m:
        return None, (v or "").strip()
    try:
        return int(m.group(1)), m.group(2)
    except ValueError:
        return None, (v or "").strip()


def _looks_like_row(row) -> bool:
    """A data row of the headerless order-guide layout: 7 fields, a numeric
    item # first, a 1-2 digit DC code, and a 14-digit timestamp last."""
    if len(row) != 7:
        return False
    item, dc, stamp = row[0].strip(), row[2].strip(), row[6].strip()
    return (item.isdigit() and len(item) >= 4
            and dc.isdigit() and len(dc) <= 2
            and stamp.isdigit() and len(stamp) == 14)


def looks_like_order_guide(csv_text: str) -> bool:
    """True when this CSV is Cheney's headerless order guide rather than the
    on-hand snapshot. Used as a guard by the on-hand CSV path."""
    reader = csv.reader(io.StringIO(csv_text or ""))
    checked = matched = 0
    for row in reader:
        if not any((c or "").strip() for c in row):
            continue
        checked += 1
        if _looks_like_row(row):
            matched += 1
        if checked >= 10:
            break
    return checked > 0 and matched == checked


def parse_order_guide(csv_text: str, *, filename: str = "OrderGuide.csv"):
    """Parse a Cheney order-guide CSV into (rows, errors, meta).

    ``rows``: one dict per catalog line --
        item_no, brand, dc_code, dc_name, warehouse, in_scope, on_hand,
        pack, case_size, pack_size, case_cost, snapshot_date, account,
        store, variety
    ``errors``: human-readable strings for the health/unparsed surfaces.
    ``meta``: file-level facts, including ``on_hand_populated`` -- False when
        the on-hand column is zero/blank on every row, which is what makes this
        file unusable as an inventory snapshot.

    Emits NO events by design; see the module docstring.
    """
    rows: list[dict] = []
    errors: list[str] = []
    account = account_from_filename(filename)
    meta: dict = {
        "filename": filename,
        "account": account,
        "store": store_from_account(account),
        "snapshot_date": snapshot_from_filename(filename),
        "dc_codes": [],
        "row_count": 0,
        "on_hand_populated": False,
        "on_hand_nonzero_rows": 0,
    }
    if not (csv_text or "").strip():
        return rows, ["cheney order guide: empty file"], meta

    raw = [r for r in csv.reader(io.StringIO(csv_text))
           if any((c or "").strip() for c in r)]
    if not raw:
        return rows, ["cheney order guide: no rows"], meta

    bad_shape = 0
    dc_codes: set[str] = set()
    for row in raw:
        if not _looks_like_row(row):
            bad_shape += 1
            continue
        item_no = _clean_code(row[0])
        brand = row[1].strip()
        dc_code = normalize_dc_code(row[2])
        warehouse = warehouse_from_dc_code(row[2])
        on_hand = opt_float(row[3])
        pack_count, pack_size = _split_pack(row[4])
        cost = opt_float(row[5])
        snap = _iso_from_stamp(row[6]) or meta["snapshot_date"]
        dc_codes.add(dc_code)
        if on_hand:
            meta["on_hand_nonzero_rows"] += 1
        if not is_known_dc(row[2]):
            errors.append(f"cheney order guide: unknown DC code {row[2]!r} "
                          f"(item {item_no})")
        rows.append({
            "item_no": item_no,
            "brand": brand,
            "dc_code": dc_code,
            "dc_name": dc_name(dc_code),
            "warehouse": warehouse,
            "in_scope": bool(warehouse),
            "on_hand": on_hand,
            "pack": row[4].strip(),
            "case_size": pack_count,
            "pack_size": pack_size,
            "case_cost": cost,
            "snapshot_date": snap,
            "account": account,
            "store": meta["store"],
            "variety": _variety("", brand, item_no),
        })

    meta["row_count"] = len(rows)
    meta["dc_codes"] = sorted(dc_codes)
    meta["on_hand_populated"] = meta["on_hand_nonzero_rows"] > 0
    if bad_shape:
        errors.append(f"cheney order guide: {bad_shape} row(s) did not match the "
                      f"expected 7-column layout and were skipped")
    if rows and not meta["on_hand_populated"]:
        errors.append(
            f"cheney order guide {filename}: on-hand column is 0 on all "
            f"{len(rows)} rows -- this is a price/catalog file, NOT an "
            f"inventory snapshot. Not applied to on-hand (doing so would zero "
            f"out real counts)."
        )
    if not rows and not errors:
        errors.append("cheney order guide: parsed no rows")
    return rows, errors, meta


def _units_per_case(pack_count, pack_size: str):
    """Units per case from the order guide's pack string.

    "001 60CT" -> 60 (one 60-count pack), "024 12OZ" -> 24 (24 cans),
    "004 5LB"  -> 4. A count size multiplies; a weight/volume size does not.
    Returns None when the pack can't be read.
    """
    if not pack_count:
        return None
    m = re.match(r"^([\d.]+)\s*CT\b", (pack_size or "").upper())
    if m:
        try:
            return int(pack_count * float(m.group(1)))
        except ValueError:
            return None
    return int(pack_count)


def to_on_hand_events(rows: list[dict], *, filename: str = "OrderGuide.csv"):
    """Turn order-guide rows into ``on_hand`` events -- ONLY valid when the
    file's on-hand column is actually populated.

    Callers must check ``meta["on_hand_populated"]`` first; a file with an
    all-zero on-hand column is a price list, and converting it would erase
    every warehouse's real count. ``cheney_csv_inventory.parse_inventory_csv``
    enforces that ordering, so route through it rather than calling this
    directly.

    Returns (events, errors) in the same shape as
    ``cheney_csv_inventory.parse_inventory_csv``.
    """
    events: list[dict] = []
    errors: list[str] = []
    # An order guide is a whole distributor catalog: ~95% of its rows are
    # third-party items the tracker doesn't model. Listing each one would push
    # ~1,800 errors per daily drop onto the health surface and bury the real
    # problems, so unmapped rows are counted and reported once.
    unmapped: list[str] = []
    untracked_dc: list[str] = []
    idx = 0
    for r in rows:
        if r["on_hand"] is None:
            continue
        if not r["variety"]:
            unmapped.append(r["item_no"])
            continue
        if not r["warehouse"]:
            untracked_dc.append(
                f"{r['variety']} at {r['dc_name'] or r['dc_code']}")
            continue
        idx += 1
        item: dict = {
            "quantity": r["on_hand"],
            "distributor": DISTRIBUTOR,
            "name": _build_name(DISTRIBUTOR, r["variety"], r["warehouse"]),
            "variety": r["variety"],
            "warehouse": r["warehouse"],
            "unit": "cs",
            "case_size": _units_per_case(r["case_size"], r["pack_size"])
                         or DEFAULT_CASE_SIZE,
            "case_cost": (r["case_cost"] if r["case_cost"] is not None
                          else CHENEY_CASE_COST),
        }
        if r["item_no"]:
            item["distributor_sku"] = r["item_no"]
        ev: dict = {
            "event_type": "on_hand",
            "item": item,
            "source_message_id": f"cheney-order-guide:{filename}#{idx}",
            "source_subject": f"Cheney daily order guide (with on-hand): {filename}",
            "po_number": "",
            "po_revision": "",
        }
        if r["snapshot_date"]:
            ev["count_date"] = r["snapshot_date"]
        events.append(ev)

    if unmapped:
        shown = ", ".join(unmapped[:5])
        more = f" (+{len(unmapped) - 5} more)" if len(unmapped) > 5 else ""
        errors.append(
            f"cheney order guide: {len(unmapped)} catalog row(s) are not H&H "
            f"items we track -- skipped: {shown}{more}. Expected: an order "
            f"guide lists Cheney's whole catalog for the account.")
    if untracked_dc:
        errors.append(
            f"cheney order guide: {len(untracked_dc)} row(s) are at a DC the "
            f"tracker doesn't model -- skipped: "
            f"{', '.join(sorted(set(untracked_dc))[:5])}")
    return events, errors


def summarize(rows: list[dict], meta: dict) -> dict:
    """Compact per-file summary for the daily feed report."""
    priced = [r for r in rows if r["case_cost"] is not None]
    varieties = sorted({r["variety"] for r in rows if r["variety"]})
    return {
        "filename": meta.get("filename", ""),
        "account": meta.get("account", ""),
        "store": meta.get("store", "") or "(unknown store)",
        "dc_codes": meta.get("dc_codes", []),
        "warehouses": sorted({r["warehouse"] for r in rows if r["warehouse"]}),
        "out_of_scope_dc": sorted({r["dc_name"] or r["dc_code"]
                                   for r in rows if not r["in_scope"]}),
        "snapshot_date": meta.get("snapshot_date", ""),
        "rows": len(rows),
        "priced_rows": len(priced),
        "hh_varieties": varieties,
        "on_hand_populated": meta.get("on_hand_populated", False),
    }


__all__ = [
    "parse_order_guide", "looks_like_order_guide", "to_on_hand_events",
    "summarize", "account_from_filename", "snapshot_from_filename",
    "DISTRIBUTOR",
]
