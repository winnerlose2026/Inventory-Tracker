"""Parser for Cheney Brothers' daily on-hand inventory CSV (SFTP feed).

Walt Wilcox (Cheney software dev) agreed 2026-07-06 to drop a per-DC daily
on-hand snapshot as CSV over SFTP. Agreed columns (header names are NOT
contract-fixed, so matched fuzzily): Cheney item #, product description,
DC/facility, on-hand cases, case size, case cost, snapshot timestamp.

Each data row -> one ``on_hand`` event dict in the exact shape
``cheney_inventory_report.parse_report_xlsx`` produces, keyed to a
(variety, warehouse) SKU via the shared item#/description -> variety resolver.
Feed the events to ``/api/email/ingest-events`` (source="cheney-sftp-csv").
This replaces the fragile weekly OCR-from-embedded-image path with a
structured daily feed.

2026-08-04 -- the first real CSV drop was NOT this file. Cheney sent a
headerless daily "OrderGuide" export (catalog + case cost, on-hand column zero
on every row); see ``cheney_order_guide``. Two guards below keep that file, and
anything shaped like it, out of the on-hand path:

  1. ``looks_like_order_guide`` -> rejected by name, with a pointer to the right
     parser, instead of failing with an opaque "columns not found".
  2. an on-hand column that is zero/blank on EVERY row is refused. A real
     snapshot always has some stock somewhere; an all-zero column means the
     column isn't populated, and applying it would overwrite every warehouse's
     real count with 0. Pass ``allow_all_zero=True`` only for a deliberate
     zero-out.

The header names themselves are still not contract-fixed, so they stay
fuzzily matched.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

try:  # package import
    from .cheney_inventory_report import (
        _variety, warehouse_from_filename, _clean_code, _build_name,
        DEFAULT_CASE_SIZE,
    )
    from .cheney_po_parser import CHENEY_CASE_COST
    from .cheney_order_guide import (
        looks_like_order_guide, parse_order_guide, to_on_hand_events)
    from .parsers._common import opt_float, opt_int
except ImportError:  # standalone (tests / CLI)
    from cheney_inventory_report import (  # type: ignore
        _variety, warehouse_from_filename, _clean_code, _build_name,
        DEFAULT_CASE_SIZE,
    )
    from cheney_po_parser import CHENEY_CASE_COST  # type: ignore
    from cheney_order_guide import (  # type: ignore
        looks_like_order_guide, parse_order_guide, to_on_hand_events)
    from parsers._common import opt_float, opt_int  # type: ignore

DISTRIBUTOR = "Cheney Brothers"


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (h or "").strip().lower())


# Units a quantity column can be in that are NOT cases. Whole words, so "bulb"
# doesn't read as "lb". _norm_header has already flattened punctuation to
# spaces, so \b behaves.
_NOT_CASES_RE = re.compile(
    r"\b(split|splits|each|eaches|unit|units|weight|lb|lbs|pound|pounds|oz|ounce|ounces)\b")


def _role_for_header(h: str) -> str:
    """Map one CSV header to a canonical role, or '' if none. Order matters:
    the most specific tests run first so e.g. 'case size' isn't grabbed as a
    quantity and 'product description' isn't grabbed as the item number."""
    s = _norm_header(h)
    if not s:
        return ""
    # description / variety (before 'item', so 'item description' -> desc)
    if "desc" in s or "variety" in s or s in ("product", "product name") or "product name" in s:
        return "desc"
    # snapshot timestamp / as-of date
    if any(k in s for k in ("timestamp", "snapshot", "as of", "asof", "count date", "count dt")) or s in ("date", "time", "datetime"):
        return "timestamp"
    # case size / pack
    if "case size" in s or "casesize" in s or "pack size" in s or "units per case" in s or s in ("pack", "size", "cs size"):
        return "case_size"
    # case cost / price
    if "case cost" in s or "casecost" in s or "unit cost" in s or s in ("cost", "price", "extended cost") or "case price" in s:
        return "case_cost"
    # on-hand quantity, in CASES. A feed that also reports splits/eaches/weight
    # on hand ("Split Units On Hand") must not win the qty role: it tends to
    # come first in the row, and binding to it both reads splits as cases and
    # trips the all-zero guard on a perfectly good snapshot. Whole words only,
    # and scoped to the qty test so it can't shadow the roles tested after it.
    if not _NOT_CASES_RE.search(s):
        if ("on hand" in s or "onhand" in s or "on hnd" in s or "full case" in s
                or s in ("cases", "qty", "quantity", "oh", "cs oh",
                         "cases on hand", "on hand cases")):
            return "qty"
    # DC / facility / warehouse
    if s in ("dc", "dc name") or any(k in s for k in ("facility", "warehouse", "location", "branch", "site")):
        return "warehouse"
    # Cheney item number / catalog (last, broad)
    if "item" in s or "catalog" in s or "sku" in s or "cheney" in s or s in ("product number", "product no", "product #"):
        return "item"
    return ""


def _to_iso_date(v: str) -> str:
    """Best-effort snapshot timestamp -> ISO 'YYYY-MM-DD' (count_date). Empty
    when unparseable, so the ingest path's fallback still applies."""
    s = (v or "").strip()
    if not s:
        return ""
    # ISO-ish already
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    # M/D/Y or M-D-Y
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", s)
    if m:
        mo, d, y = (int(x) for x in m.groups())
        if y < 100:
            y += 2000
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return ""


def parse_inventory_csv(csv_text: str, *, filename: str = "cheney_inventory.csv",
                        allow_all_zero: bool = False):
    """Parse a Cheney daily on-hand CSV into (events, errors).

    ``events`` are ``on_hand`` event dicts ready for /api/email/ingest-events.
    ``errors`` are human-readable strings for the unparsed/health surfaces.

    Set ``allow_all_zero=True`` only to deliberately zero out counts; by
    default a file whose on-hand column is zero on every row is refused (see
    the module docstring).
    """
    events: list[dict] = []
    errors: list[str] = []
    if not (csv_text or "").strip():
        return events, ["cheney inventory csv: empty file"]

    # Cheney's headerless order-guide export. Its column layout is known, so
    # the deciding question is whether the on-hand column is actually
    # populated:
    #   * populated -> this IS the daily snapshot we've been waiting for;
    #     convert it and carry on.
    #   * all zero  -> a price list. Refuse: applying it would write 0 cases
    #     for every item at every warehouse.
    if looks_like_order_guide(csv_text):
        og_rows, og_errors, og_meta = parse_order_guide(csv_text, filename=filename)
        og_events, conv_errors = to_on_hand_events(og_rows, filename=filename)
        # The test that matters is whether the rows we'd actually WRITE carry
        # stock -- not whether anything in the 236-row catalog does. Cheney
        # distributes ~220 third-party items alongside the ~12 H&H SKUs the
        # tracker models; a nonzero count on a case of Coke says nothing about
        # whether the bagel rows are populated, and gating on it would write 0
        # over every real bagel count.
        writable_nonzero = any((e["item"]["quantity"] or 0) > 0 for e in og_events)
        if writable_nonzero or allow_all_zero:
            shape_errors = [e for e in og_errors if "NOT an" not in e]
            return og_events, shape_errors + conv_errors
        head = (f"cheney inventory csv {filename}: this is Cheney's headerless "
                f"OrderGuide export (catalog + case cost)")
        if og_events:
            return [], [
                f"{head}, and on-hand is 0 for all {len(og_events)} tracked "
                f"item(s) in it -- refused, since applying it would zero out "
                f"those counts. Parse it with "
                f"integrations.cheney_order_guide.parse_order_guide, or re-run "
                f"scripts/ingest_cheney_inventory_csv.py with --allow-all-zero "
                f"for a deliberate zero-out."
            ]
        if og_meta["on_hand_populated"]:
            return [], [
                f"{head}. It does carry on-hand quantities, but none of those "
                f"rows map to an H&H item at a tracked DC, so there is nothing "
                f"to apply."
            ] + conv_errors
        return [], [
            f"{head} with no on-hand quantities at all -- nothing to apply. "
            f"Parse it with integrations.cheney_order_guide.parse_order_guide."
        ]

    reader = csv.reader(io.StringIO(csv_text))
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        return events, ["cheney inventory csv: no rows"]

    header = rows[0]
    roles: dict[str, int] = {}
    for j, h in enumerate(header):
        role = _role_for_header(h)
        if role and role not in roles:
            roles[role] = j
    if "qty" not in roles or ("item" not in roles and "desc" not in roles):
        return events, [
            "cheney inventory csv: could not find the required columns "
            f"(need an on-hand/cases column + item# or description); saw headers {header!r}"
        ]

    def cell(row, role):
        j = roles.get(role)
        return (row[j].strip() if (j is not None and j < len(row)) else "")

    # Guard 2: refuse a snapshot whose on-hand column is zero/blank on EVERY
    # row. A real count has stock somewhere; an all-zero column means Cheney
    # isn't populating it, and applying it would erase every warehouse's count.
    data_rows = rows[1:]
    if data_rows and not allow_all_zero:
        seen_qty = [opt_float(cell(r, "qty")) for r in data_rows]
        if not any(q for q in seen_qty if q):
            return events, [
                f"cheney inventory csv {filename}: the on-hand column "
                f"({header[roles['qty']]!r}) is zero or blank on all "
                f"{len(data_rows)} row(s) -- refused rather than zeroing out "
                f"every warehouse's count. If this really is a full zero-out, "
                f"re-run scripts/ingest_cheney_inventory_csv.py with "
                f"--allow-all-zero."
            ]

    idx = 0
    for row in rows[1:]:
        item_no = cell(row, "item")
        desc = cell(row, "desc")
        wh_raw = cell(row, "warehouse")
        variety = _variety("", desc, item_no)
        warehouse = warehouse_from_filename(wh_raw)
        qty = opt_float(cell(row, "qty"))
        if not variety:
            if qty is not None or desc or _clean_code(item_no):
                errors.append(f"cheney csv: unmapped row (item={_clean_code(item_no)!r}, desc={desc!r})")
            continue
        if not warehouse:
            errors.append(f"cheney csv: unknown DC {wh_raw!r} for {variety} (item={_clean_code(item_no)!r})")
            continue
        if qty is None:
            continue
        cs = opt_int(cell(row, "case_size")) or DEFAULT_CASE_SIZE
        cost = opt_float(cell(row, "case_cost"))
        idx += 1
        item: dict = {
            "quantity": qty,
            "distributor": DISTRIBUTOR,
            "name": _build_name(DISTRIBUTOR, variety, warehouse),
            "variety": variety,
            "warehouse": warehouse,
            "unit": "cs",
            "case_size": cs,
            "case_cost": cost if cost is not None else CHENEY_CASE_COST,
        }
        if item_no:
            item["distributor_sku"] = _clean_code(item_no)
        ev: dict = {
            "event_type": "on_hand",
            "item": item,
            "source_message_id": f"cheney-csv:{filename}#{idx}",
            "source_subject": f"Cheney daily on-hand CSV: {filename}",
            "po_number": "",
            "po_revision": "",
        }
        cd = _to_iso_date(cell(row, "timestamp"))
        if cd:
            ev["count_date"] = cd
        events.append(ev)

    if not events and not errors:
        errors.append("cheney inventory csv: parsed no on-hand rows")
    return events, errors
