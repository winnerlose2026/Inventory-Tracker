#!/usr/bin/env python3
"""Inventory Tracker with Usage History"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


DATA_DIR = Path("data")
INVENTORY_FILE = DATA_DIR / "inventory.json"
USAGE_FILE = DATA_DIR / "usage.json"


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _load(path: Path) -> dict | list:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {} if path == INVENTORY_FILE else []


def _save(path: Path, data):
    DATA_DIR.mkdir(exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_inventory() -> dict:
    inv = _load(INVENTORY_FILE)
    # The order of these passes matters:
    #   1. Rebase ordered_at on legacy entries (uses email subject as a
    #      proxy for the PO date when the parser didn't supply one).
    #   2. Collapse cross-revision dupes — keeps only the highest
    #      po_revision per (SKU, po_number). Catches data that landed
    #      before the apply-path supersede logic existed.
    #   3. Dedup identical (po, rev, qty) entries on the same SKU.
    #   4. Rollover entries whose newly-correct eta is in the past
    #      into the SKU's quantity. Must run AFTER rebase, otherwise
    #      backlogged POs would stay pending until ingest_time + 30d.
    rebased = _rebase_ordered_at_from_subject(inv)
    rev_collapsed = _collapse_revision_dupes(inv)
    deduped = _dedup_on_order(inv)
    rolled = _rollover_on_order(inv)
    if rebased or rev_collapsed or deduped or rolled:
        usage = _load(USAGE_FILE) if rolled else None
        if rolled:
            _append_rollover_usage(inv, usage)
        _save(INVENTORY_FILE, inv)
        if rolled:
            _save(USAGE_FILE, usage)
    return inv


def save_inventory(inv: dict):
    _save(INVENTORY_FILE, inv)


def load_usage() -> list:
    return _load(USAGE_FILE)


def save_usage(usage: list):
    _save(USAGE_FILE, usage)


# Daily production sheets — separate from inventory because they are a
# log of what was BAKED for a given PO, not a state of on-hand stock.
PRODUCTION_FILE = DATA_DIR / "production.json"


def load_production() -> list:
    if PRODUCTION_FILE.exists():
        with open(PRODUCTION_FILE) as f:
            return json.load(f)
    return []


def save_production(records: list):
    DATA_DIR.mkdir(exist_ok=True)
    with open(PRODUCTION_FILE, "w") as f:
        json.dump(records, f, indent=2)


# Bakery labor — feeds the $PLH report on the Report page. One entry per
# date {date: YYYY-MM-DD, hours: float, dollars: float, source: str}.
# `source` is informational (e.g. "toast:mvt-dc", "manual-upload") so we
# can re-seed from a different source later without losing audit trail.
LABOR_FILE = DATA_DIR / "labor.json"


def load_labor() -> list:
    if LABOR_FILE.exists():
        with open(LABOR_FILE) as f:
            return json.load(f)
    return []


def save_labor(entries: list):
    DATA_DIR.mkdir(exist_ok=True)
    with open(LABOR_FILE, "w") as f:
        json.dump(entries, f, indent=2)


# Canceled POs — when a distributor cancels an order, we record the PO
# number here so the email scanner skips it on future runs (instead of
# re-ingesting from a still-sitting source email and re-creating
# on_order entries we just removed).
CANCELED_POS_FILE = DATA_DIR / "canceled_pos.json"

# Chefs Warehouse POs live in their own file so they never touch the
# Inventory tab. The Pending POs tab merges them in for display, but
# inventory.json never carries a "Chefs Warehouse" item. One entry
# per PO; the scanner replaces by po_number on re-ingest.
CHEFS_WAREHOUSE_POS_FILE = DATA_DIR / "chefs_warehouse_pos.json"


def load_chefs_warehouse_pos() -> list:
    """Return the list of Chefs Warehouse POs (empty list if none).

    Schema (one dict per PO):
        po_number, po_revision, distributor (always "Chefs Warehouse"),
        warehouse ("<City>, <ST>"), dc_code (NY/MD/FLA/CHI),
        ship_to_id, ship_to_name, ship_to_city, ship_to_state,
        ship_to_zip, order_date, delivery_date, buyer_id, buyer_name,
        total_usd, total_cs,
        lines: [{line_no, vendor_item, cw_item, description, variety,
                 sliced, pack, pack_um, case_size, qty, unit,
                 unit_cost, ext_cost}],
        ordered_at, eta, ship_date, arrival_date,    # PO lifecycle
        source, source_subject, source_message_id,    # provenance
        ingested_at,
        canceled (optional bool), canceled_at, canceled_reason
    """
    if CHEFS_WAREHOUSE_POS_FILE.exists():
        with open(CHEFS_WAREHOUSE_POS_FILE) as f:
            try:
                return json.load(f)
            except Exception:
                return []
    return []


def save_chefs_warehouse_pos(records: list) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(CHEFS_WAREHOUSE_POS_FILE, "w") as f:
        json.dump(records, f, indent=2)

# Toast POS sales — per-location, per-day, per-item product mix.
# One entry per (restaurant_guid, business_date, item_guid).
# Used by the Report page Top Consumed section.
SALES_FILE = DATA_DIR / "sales.json"

# Retail location registry — every Toast restaurant the integration is
# approved on, excluding DCs and disabled locations. Surfaces in the
# Top Consumed location dropdown even before sales rows have been
# ingested for a location, so the user can pick any location for
# comparison. Source: `toast_list_restaurants` (snapshot 2026-05-15).
# status values:
#   "active"           — has retrievable per-day product mix
#   "pre_launch"       — $0 lifetime sales (location not yet operating)
#   "data_unavailable" — chain rollup shows revenue but ordersBulk returns
#                        0 orders across all sampled dates. Verified
#                        2026-05-15 via toast_list_orders (returned 0
#                        for Chapel Hill across Q1 2025) and via
#                        toast_backfill_cache --force (claimed cached
#                        in 0.1s, but product_mix still 0). Conclusion:
#                        the chain_revenue aggregate is a phantom cache
#                        entry from a prior Toast partner integration;
#                        the current credentials have no order-level
#                        access to these locations. Action: ask Toast
#                        support whether the current partner_id covers
#                        these GUIDs, or migrate the cache via a fresh
#                        integration that does.
TOAST_RETAIL_LOCATIONS: list[dict] = [
    {"restaurant_guid": "0d5af5fb-c12a-4f47-ac4e-fd1110b91dcb", "location": "Palm Beach Gardens - Avenir Center", "state": "FL", "status": "pre_launch"},
    {"restaurant_guid": "12b6706d-bf0f-4405-a493-51929a4e9dcd", "location": "Pinecrest",                            "state": "FL", "status": "active"},
    {"restaurant_guid": "13339585-284c-40cd-8a9b-01789ea875e6", "location": "UES",                                  "state": "NY", "status": "active"},
    {"restaurant_guid": "25192c92-06ef-4c13-8805-e434a9cd7fa8", "location": "Penn Station",                         "state": "NY", "status": "active"},
    {"restaurant_guid": "26642447-d72b-4ea2-8811-1da88cc463d0", "location": "Knoxville",                            "state": "TN", "status": "active"},
    {"restaurant_guid": "386b0bcc-869a-4b29-8837-14d14b4c65c7", "location": "Altamonte Springs",                    "state": "FL", "status": "active"},
    {"restaurant_guid": "5167cd50-baaa-4bd4-8cda-8c9b52724b61", "location": "UWS",                                  "state": "NY", "status": "active"},
    {"restaurant_guid": "51e1cd33-2102-4795-8557-004f41c5d9c9", "location": "West Palm Beach - Nora District",      "state": "FL", "status": "active"},
    {"restaurant_guid": "6775f38e-0dda-4edd-b2ff-926783059edc", "location": "Santa Monica",                         "state": "CA", "status": "active"},
    {"restaurant_guid": "69c78fdf-e35c-403a-931f-e29568fefa83", "location": "Westlake",                             "state": "CA", "status": "pre_launch"},
    {"restaurant_guid": "83239e6c-3d1b-44b9-8ad3-124492636812", "location": "Palm Desert",                          "state": "CA", "status": "active"},
    {"restaurant_guid": "990a7e59-34f9-406f-bbc1-bd8594174000", "location": "Mandarin",                             "state": "FL", "status": "active"},
    {"restaurant_guid": "99e38e88-9268-4e29-9c9e-05b99cad8e23", "location": "Irvine",                               "state": "CA", "status": "pre_launch"},
    {"restaurant_guid": "9a5f428b-16db-43ab-9237-6dff70516a74", "location": "River Oaks",                           "state": "TX", "status": "pre_launch"},
    {"restaurant_guid": "9da7193b-1720-4f83-949a-4124a544a92a", "location": "Fulton Market",                        "state": "IL", "status": "pre_launch"},
    {"restaurant_guid": "ab100b40-c9e0-4831-9f94-31020a66e6bc", "location": "St Johns Jacksonville",                "state": "FL", "status": "active"},
    {"restaurant_guid": "bfd2363b-47b9-4299-b150-720c81ff65f4", "location": "Fort Lauderdale - Flagler Village",    "state": "FL", "status": "pre_launch"},
    {"restaurant_guid": "c1432aac-3a03-4693-acf2-55b8d116a80a", "location": "South Tampa",                          "state": "FL", "status": "active"},
    {"restaurant_guid": "d725bcfd-8cd5-4800-a3ab-31a50b29173a", "location": "Kips Bay",                             "state": "NY", "status": "active"},
    {"restaurant_guid": "dc9a30b0-6ebb-4c8c-9578-16a23cee2e99", "location": "Boca Raton - Glades Plaza",            "state": "FL", "status": "active"},
    {"restaurant_guid": "dd987834-83cf-4960-b161-5482ee99e8e1", "location": "MTH",                                  "state": "NY", "status": "active"},
    {"restaurant_guid": "fba3457a-1b45-4b81-af83-7b5674fd0d8f", "location": "Echo Park",                            "state": "CA", "status": "active"},
    {"restaurant_guid": "fc57f65c-7746-44c4-b1e9-15a022e34dfc", "location": "Chapel Hill",                          "state": "NC", "status": "active"},
]



def load_sales() -> list:
    if SALES_FILE.exists():
        with open(SALES_FILE) as f:
            try:
                return json.load(f)
            except Exception:
                return []
    return []


def save_sales(entries: list):
    DATA_DIR.mkdir(exist_ok=True)
    with open(SALES_FILE, "w") as f:
        json.dump(entries, f, indent=2)


# Bakery weekly sales -- fed from the weekly "Bakery Model - Sales v. Labor"
# spreadsheet while the production bakery is not yet wired up to Toast.
# One entry per ISO week (Mon-Sun) carrying the weekly channel split and
# the total. Daily granularity is intentionally NOT stored here because
# the source spreadsheet only carries weekly truth for sales (daily cells
# are weekly_total / 7).
BAKERY_SALES_FILE = DATA_DIR / "bakery_sales.json"


def load_bakery_sales() -> list:
    if BAKERY_SALES_FILE.exists():
        with open(BAKERY_SALES_FILE) as f:
            try:
                return json.load(f)
            except Exception:
                return []
    return []


def save_bakery_sales(entries: list):
    DATA_DIR.mkdir(exist_ok=True)
    with open(BAKERY_SALES_FILE, "w") as f:
        json.dump(entries, f, indent=2)



def load_canceled_pos() -> dict:
    """Return {po_number: {canceled_at, reason, note}}."""
    if CANCELED_POS_FILE.exists():
        with open(CANCELED_POS_FILE) as f:
            try:
                return json.load(f)
            except Exception:
                return {}
    return {}


def save_canceled_pos(data: dict):
    DATA_DIR.mkdir(exist_ok=True)
    with open(CANCELED_POS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def is_po_canceled(po_number: str) -> bool:
    if not po_number:
        return False
    return po_number.strip() in load_canceled_pos()



# ---------------------------------------------------------------------------
# On-order rollover
# ---------------------------------------------------------------------------
# PO-tagged restock events from email scans are parked in item["on_order"]
# with an ETA = ordered_at + lead time. When the ETA passes we promote them
# into item["quantity"] and append a matching usage entry so history stays
# consistent. This runs on every load_inventory() so readers always see
# current state without needing a separate scheduler.

# Staged arrivals that have been promoted get stashed here between the
# _rollover_on_order pass (which mutates the inventory dict) and the
# _append_rollover_usage pass (which mutates the usage list). Not thread-safe
# — we rely on the single-process Flask dev/gunicorn model.
_PENDING_ROLLOVER_AUDIT: list = []


def _rollover_trigger(entry: dict) -> "datetime | None":
    """Resolve the effective rollover trigger for a pending on_order entry.

    Rule of precedence (per the dashboard's ship-date feature):
      1. ``arrival_date`` if set — set to ship_date + 7 days when an
         operator records a ship date on the PO. Supersedes the 30-day
         ETA rule.
      2. ``eta`` otherwise — the default ordered_at + lead_days date.

    Returns the parsed datetime, or None if neither field is parseable.
    """
    arrival = (entry.get("arrival_date") or "").strip()
    if arrival:
        try:
            return datetime.fromisoformat(arrival)
        except ValueError:
            pass
    eta = (entry.get("eta") or "").strip()
    if eta:
        try:
            return datetime.fromisoformat(eta)
        except ValueError:
            pass
    return None


def _rollover_on_order(inv: dict) -> bool:
    """Promote on_order entries whose effective trigger has passed into
    quantity. Mutates ``inv`` in place. Returns True if any entry was
    promoted.

    The trigger is normally the entry's ``eta`` (ordered_at + 30 days).
    If an operator sets a ship_date via /api/on-order/ship-date,
    arrival_date = ship_date + 7 days is stored on the entry and used
    INSTEAD of eta as the trigger. Promotion is idempotent: once an
    entry has been added to quantity and the entry removed from
    on_order it cannot be added again."""
    global _PENDING_ROLLOVER_AUDIT
    _PENDING_ROLLOVER_AUDIT = []
    now = datetime.now()
    changed = False
    for key, item in inv.items():
        pending = item.get("on_order") or []
        if not pending:
            continue
        kept = []
        for entry in pending:
            trigger = _rollover_trigger(entry)
            if trigger is None:
                # No usable trigger date — leave pending so an operator
                # can correct it (typically via /api/on-order/ship-date).
                kept.append(entry)
                continue
            if trigger > now:
                kept.append(entry)
                continue
            qty = float(entry.get("qty") or 0)
            if qty <= 0:
                changed = True
                continue
            item["quantity"] = float(item.get("quantity", 0)) + qty
            item["updated"] = now.isoformat()
            _PENDING_ROLLOVER_AUDIT.append({
                "item_key": key,
                "item_name": item.get("name", key),
                "unit": item.get("unit", ""),
                "qty": qty,
                "po_number": entry.get("po_number", ""),
                "po_revision": entry.get("po_revision", ""),
                "eta": trigger.isoformat(),
                "timestamp": now.isoformat(),
            })
            changed = True
        item["on_order"] = kept
    return changed


_USF_DATE_RE = __import__("re").compile(r"\b(\d{2})/(\d{2})/(\d{2})\b")
_LEAD_DAYS_DEFAULT = 30


def _po_lead_days_local() -> int:
    """Mirror sync_inventory._po_lead_days() without the circular import."""
    import os
    try:
        return int(os.environ.get("PO_LEAD_DAYS", _LEAD_DAYS_DEFAULT))
    except (TypeError, ValueError):
        return _LEAD_DAYS_DEFAULT


def _rebase_ordered_at_from_subject(inv: dict) -> bool:
    """Backfill ``ordered_at`` from the PO date embedded in source_subject.

    Historical on_order entries store ``ordered_at`` = ingestion time,
    but the real PO order date sits in the email subject for US Foods
    POs (e.g. ``USF PO 533457 4C/4120 04/28/26 ...``). When we can
    parse a date out of the subject, rewrite ordered_at + eta so the
    rollover into quantity tracks real lead time. Cheney subjects
    don't carry the date, so those entries are left alone (future
    ingests will populate ``po_order_date`` directly via the parser).

    Returns True if anything changed.
    """
    lead = _po_lead_days_local()
    changed = False
    for key, item in inv.items():
        for entry in (item.get("on_order") or []):
            subj = entry.get("source_subject") or ""
            m = _USF_DATE_RE.search(subj)
            if not m:
                continue
            mm, dd, yy = m.groups()
            year = 2000 + int(yy) if int(yy) < 70 else 1900 + int(yy)
            iso = f"{year:04d}-{int(mm):02d}-{int(dd):02d}T00:00:00"
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                continue
            new_ordered_at = dt.isoformat()
            if entry.get("ordered_at") == new_ordered_at:
                continue
            entry["ordered_at"] = new_ordered_at
            entry["eta"] = (dt + timedelta(days=lead)).isoformat()
            changed = True
    return changed


def _collapse_revision_dupes(inv: dict) -> bool:
    """When the same SKU has multiple pending entries for the same
    ``po_number`` but different ``po_revision``, keep only the highest
    revision. The supersede logic in sync_inventory normally handles
    this at apply time, but legacy data can still carry pre-supersede
    entries (e.g. revision 0000001 + 0000002 of the same line both
    sitting pending). Returns True if anything changed.

    Same SKU + same PO + same revision but different qty (legit
    separate lines on a single PO with repeats) are preserved.
    """
    changed = False
    for key, item in inv.items():
        pending = item.get("on_order") or []
        if len(pending) < 2:
            continue
        by_po: dict = {}
        for entry in pending:
            by_po.setdefault(entry.get("po_number") or "", []).append(entry)
        kept = []
        for po, entries in by_po.items():
            if len(entries) < 2 or not po:
                kept.extend(entries)
                continue
            # Map po_revision -> integer using the same precedence as
            # sync_inventory._po_rev_int: numeric strings -> their int,
            # non-numeric tokens like "REPRINT" -> a large sentinel so
            # they always supersede prior numbered revisions (matches
            # USF's actual issuing semantics).
            _REPRINT_SENTINEL = 10_000_000
            def _rev_int(e):
                s = str(e.get("po_revision") or "").strip()
                if not s:
                    return 0
                try:
                    return int(s.lstrip("0") or "0")
                except (TypeError, ValueError):
                    return _REPRINT_SENTINEL
            max_rev = max(_rev_int(e) for e in entries)
            survivors = [e for e in entries if _rev_int(e) == max_rev]
            dropped = len(entries) - len(survivors)
            if dropped:
                changed = True
            kept.extend(survivors)
        if changed:
            kept.sort(key=lambda e: (e.get("ordered_at") or ""))
            item["on_order"] = kept
    return changed


def _dedup_on_order(inv: dict) -> bool:
    """Collapse duplicate pending on_order entries within each item.

    A duplicate is two or more entries with the same
    (po_number, po_revision, qty) on the same SKU. Keeps the entry with
    the earliest ``ordered_at`` so downstream ETAs stay anchored to the
    first booking. Mutates ``inv`` in place. Returns True if anything was
    changed.

    This is belt-and-suspenders to the dedup that already runs in
    ``sync_inventory._apply_events``: that one prevents new duplicates
    from being booked. This one cleans up duplicates that slipped
    through in the past (historical data, manual API posts, replays
    before the apply-path dedup landed) every time the inventory is
    loaded, so the on_order column self-heals.
    """
    changed = False
    for key, item in inv.items():
        pending = item.get("on_order") or []
        if len(pending) < 2:
            continue
        groups: dict = {}
        for entry in pending:
            line_key = (
                str(entry.get("po_number") or ""),
                str(entry.get("po_revision") or ""),
                round(float(entry.get("qty") or 0), 4),
            )
            groups.setdefault(line_key, []).append(entry)
        if all(len(v) == 1 for v in groups.values()):
            continue
        kept = []
        for entries in groups.values():
            if len(entries) == 1:
                kept.append(entries[0])
                continue
            # Two or more identical-line entries: keep the earliest by
            # ordered_at; the timestamps are usually identical when this
            # came from a single scan, in which case the sort is a no-op.
            entries.sort(key=lambda e: (e.get("ordered_at") or ""))
            kept.append(entries[0])
            changed = True
        # Preserve a stable order so the UI doesn't shuffle on every load.
        kept.sort(key=lambda e: (e.get("ordered_at") or ""))
        item["on_order"] = kept
    return changed


def _append_rollover_usage(inv: dict, usage: list) -> None:
    """Append a usage-log entry for each promoted on_order entry."""
    for audit in _PENDING_ROLLOVER_AUDIT:
        usage.append({
            "item_key": audit["item_key"],
            "item_name": audit["item_name"],
            "amount": -audit["qty"],  # negative = restock in the log convention
            "unit": audit["unit"],
            "note": (f"PO {audit['po_number']} arrived (ETA {audit['eta'][:10]})"
                     if audit["po_number"] else "On-order arrival"),
            "timestamp": audit["timestamp"],
            "po_number": audit["po_number"],
            "po_revision": audit["po_revision"],
            "source": "on_order_rollover",
        })
    _PENDING_ROLLOVER_AUDIT.clear()


# ---------------------------------------------------------------------------
# Unit migration: each -> cs
# ---------------------------------------------------------------------------
# Original seed stored quantities, thresholds, weekly_usage in individual
# bagels (unit="each"). PO parsers always emit case quantities, which meant
# applied restocks were 60x undercount-as-stock. This converts in place; the
# units_migrated flag makes it idempotent.

def migrate_units_to_case(inv: dict) -> dict:
    converted = 0
    rounded = 0
    skipped_no_case_size = 0
    for item in inv.values():
        case_size = float(item.get("case_size") or 0)
        already_cs = item.get("units_migrated") or item.get("unit") == "cs"

        if not already_cs:
            if case_size <= 0:
                skipped_no_case_size += 1
                continue
            case_cost = float(item.get("case_cost") or 0)
            item["quantity"] = float(item.get("quantity") or 0) / case_size
            item["low_stock_threshold"] = (
                float(item.get("low_stock_threshold") or 0) / case_size)
            item["weekly_usage"] = (
                float(item.get("weekly_usage") or 0) / case_size)
            if case_cost > 0:
                item["price"] = round(case_cost, 2)
            item["unit"] = "cs"
            # on_order qty is already in cases (PO parser unit); just relabel.
            for o in (item.get("on_order") or []):
                o["unit"] = "cs"
            item["units_migrated"] = True
            converted += 1

        # Cases are whole numbers. Round on-hand, threshold, and pending
        # on-order qty to integers; weekly_usage stays a float (it's a rate).
        before_qty = float(item.get("quantity") or 0)
        before_thr = float(item.get("low_stock_threshold") or 0)
        item["quantity"] = int(round(before_qty))
        item["low_stock_threshold"] = int(round(before_thr))
        item["weekly_usage"] = round(float(item.get("weekly_usage") or 0), 1)
        for o in (item.get("on_order") or []):
            o["qty"] = int(round(float(o.get("qty") or 0)))
        if (item["quantity"] != before_qty
                or item["low_stock_threshold"] != before_thr):
            rounded += 1

    return {
        "converted": converted,
        "rounded": rounded,
        "skipped_no_case_size": skipped_no_case_size,
        "total": len(inv),
    }


def add_item(name: str, quantity: float, unit: str, category: str = "general",
             low_stock_threshold: float = 5.0, price: float = 0.0,
             distributor: str = "", warehouse: str = "",
             case_cost: float = 0.0, case_size: int = 0,
             weekly_usage: float = 0.0):
    inv = load_inventory()
    key = name.lower().strip()
    if key in inv:
        print(f"  Item '{name}' already exists. Use 'update' to modify it.")
        return
    inv[key] = {
        "name": name,
        "quantity": quantity,
        "unit": unit,
        "category": category,
        "low_stock_threshold": low_stock_threshold,
        "price": price,
        "distributor": distributor,
        "warehouse": warehouse,
        "case_cost": case_cost,
        "case_size": case_size,
        "weekly_usage": weekly_usage,
        "added": datetime.now().isoformat(),
    }
    save_inventory(inv)
    print(f"  Added '{name}': {quantity} {unit}")


def update_item(name: str, quantity: Optional[float] = None,
                unit: Optional[str] = None, category: Optional[str] = None,
                low_stock_threshold: Optional[float] = None,
                price: Optional[float] = None,
                distributor: Optional[str] = None,
                warehouse: Optional[str] = None,
                case_cost: Optional[float] = None,
                case_size: Optional[int] = None,
                weekly_usage: Optional[float] = None):
    inv = load_inventory()
    key = name.lower().strip()
    if key not in inv:
        print(f"  Item '{name}' not found.")
        return
    item = inv[key]
    if quantity is not None:
        item["quantity"] = quantity
    if unit is not None:
        item["unit"] = unit
    if category is not None:
        item["category"] = category
    if low_stock_threshold is not None:
        item["low_stock_threshold"] = low_stock_threshold
    if price is not None:
        item["price"] = price
    if distributor is not None:
        item["distributor"] = distributor
    if warehouse is not None:
        item["warehouse"] = warehouse
    if case_cost is not None:
        item["case_cost"] = case_cost
    if case_size is not None:
        item["case_size"] = case_size
    if weekly_usage is not None:
        item["weekly_usage"] = weekly_usage
    item["updated"] = datetime.now().isoformat()
    save_inventory(inv)
    print(f"  Updated '{name}'.")


def restock(name: str, amount: float, note: str = ""):
    inv = load_inventory()
    key = name.lower().strip()
    if key not in inv:
        print(f"  Item '{name}' not found.")
        return
    inv[key]["quantity"] += amount
    inv[key]["updated"] = datetime.now().isoformat()
    save_inventory(inv)
    _record_usage(key, inv[key]["name"], -amount, inv[key]["unit"],
                  note or f"Restocked +{amount}")
    print(f"  Restocked '{name}' by {amount} {inv[key]['unit']}. "
          f"New total: {inv[key]['quantity']}")


def record_usage(name: str, amount: float, note: str = ""):
    inv = load_inventory()
    key = name.lower().strip()
    if key not in inv:
        print(f"  Item '{name}' not found.")
        return
    item = inv[key]
    if item["quantity"] < amount:
        print(f"  Warning: only {item['quantity']} {item['unit']} available "
              f"(tried to use {amount}).")
        return
    item["quantity"] -= amount
    item["updated"] = datetime.now().isoformat()
    save_inventory(inv)
    _record_usage(key, item["name"], amount, item["unit"], note)
    print(f"  Used {amount} {item['unit']} of '{name}'. "
          f"Remaining: {item['quantity']}")
    if item["quantity"] <= item["low_stock_threshold"]:
        print(f"  *** LOW STOCK ALERT: '{name}' is at or below threshold "
              f"({item['low_stock_threshold']} {item['unit']}) ***")


def _record_usage(key: str, display_name: str, amount: float,
                  unit: str, note: str):
    usage = load_usage()
    usage.append({
        "item_key": key,
        "item_name": display_name,
        "amount": amount,      # positive = consumed, negative = restocked
        "unit": unit,
        "note": note,
        "timestamp": datetime.now().isoformat(),
    })
    save_usage(usage)


def reverse_usage(timestamp: str) -> dict:
    """Reverse a usage/restock entry identified by its ISO timestamp.

    Effects:
      - Inverts the original entry's effect on item quantity (clamped at 0).
      - Marks the original entry as `reversed: true` (kept for audit).
      - Appends a new usage record with `source: "reversal"` so the action
        shows up in the activity log.
      - Refuses to double-reverse, reverse a reversal record, or operate on
        a missing item.
    """
    inv = load_inventory()
    usage = load_usage()

    target = None
    for entry in usage:
        if entry.get("timestamp") == timestamp:
            target = entry
            break
    if target is None:
        return {"ok": False, "error": "Activity entry not found."}
    if target.get("reversed"):
        return {"ok": False, "error": "This entry has already been reversed."}
    if target.get("source") == "reversal":
        return {"ok": False, "error": "Cannot reverse a reversal record."}

    key = target.get("item_key", "")
    if key not in inv:
        return {"ok": False,
                "error": f"Item '{target.get('item_name', key)}' "
                         f"is no longer in inventory."}

    item = inv[key]
    amount = float(target.get("amount") or 0)
    # The log convention: amount > 0 = use (qty was decreased by `amount`);
    # amount < 0 = restock (qty was increased by `abs(amount)`). The original
    # effect on inventory is therefore -amount, so the reversal effect is
    # +amount. Clamp at 0 to keep on-hand non-negative.
    new_qty = float(item.get("quantity", 0)) + amount
    item["quantity"] = max(0, new_qty)
    now_iso = datetime.now().isoformat()
    item["updated"] = now_iso

    target["reversed"] = True
    target["reversed_at"] = now_iso

    original_note = (target.get("note") or "").strip()
    short_ts = target["timestamp"][:19].replace("T", " ")
    if original_note:
        reversal_note = f"Reversed [{short_ts}]: {original_note}"
    else:
        reversal_note = f"Reversed entry from {short_ts}"

    # Reversal log entry — sign flipped from the original so the running
    # "Top Consumed/Restocked" totals stay correct.
    usage.append({
        "item_key": key,
        "item_name": target.get("item_name", item.get("name", key)),
        "amount": -amount,
        "unit": target.get("unit", item.get("unit", "")),
        "note": reversal_note,
        "timestamp": now_iso,
        "source": "reversal",
        "reverses_timestamp": target["timestamp"],
    })

    save_inventory(inv)
    save_usage(usage)
    return {
        "ok": True,
        "item_name": target.get("item_name", ""),
        "new_quantity": item["quantity"],
        "reversed_amount": amount,
    }


def remove_item(name: str):
    inv = load_inventory()
    key = name.lower().strip()
    if key not in inv:
        print(f"  Item '{name}' not found.")
        return
    del inv[key]
    save_inventory(inv)
    print(f"  Removed '{name}' from inventory.")


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _bar(value: float, maximum: float, width: int = 20) -> str:
    if maximum == 0:
        return " " * width
    filled = int(round(value / maximum * width))
    filled = max(0, min(filled, width))
    return "#" * filled + "-" * (width - filled)


def show_inventory(category: Optional[str] = None):
    inv = load_inventory()
    if not inv:
        print("  Inventory is empty. Use 'add' to add items.")
        return

    # Filter
    items = list(inv.values())
    if category:
        items = [i for i in items if i["category"].lower() == category.lower()]
        if not items:
            print(f"  No items in category '{category}'.")
            return

    # Group by category
    cats: dict[str, list] = {}
    for item in items:
        cats.setdefault(item["category"], []).append(item)

    max_qty = max((i["quantity"] for i in items), default=1) or 1

    print()
    print("=" * 72)
    print(f"  {'INVENTORY':^68}")
    print("=" * 72)
    for cat, cat_items in sorted(cats.items()):
        print(f"\n  [{cat.upper()}]")
        print(f"  {'Name':<40} {'Qty':>6} {'Distributor':<18} {'Warehouse':<20} {'Price':>7}  Alert")
        print("  " + "-" * 100)
        for item in sorted(cat_items, key=lambda x: (x.get("distributor", ""), x.get("warehouse", ""), x["name"])):
            alert = "(!)" if item["quantity"] <= item["low_stock_threshold"] else "   "
            price = f"${item['price']:.2f}" if item["price"] else "  -   "
            dist = (item.get("distributor") or "—")[:18]
            wh = (item.get("warehouse") or "—")[:20]
            name = item["name"][:40]
            print(f"  {name:<40} {item['quantity']:>6.1f} {dist:<18} {wh:<20} {price:>7}  {alert}")
    print()
    print("=" * 72)
    low = [i["name"] for i in items if i["quantity"] <= i["low_stock_threshold"]]
    if low:
        print(f"  LOW STOCK: {', '.join(low)}")
    print(f"  Total items: {len(items)}")
    print()


def show_usage(name: Optional[str] = None, limit: int = 20):
    usage = load_usage()
    if not usage:
        print("  No usage records yet.")
        return

    entries = usage
    if name:
        key = name.lower().strip()
        entries = [e for e in entries if e["item_key"] == key]
        if not entries:
            print(f"  No usage records for '{name}'.")
            return

    # Show most recent first
    entries = list(reversed(entries))[:limit]

    print()
    print("=" * 72)
    print(f"  {'USAGE HISTORY':^68}")
    print("=" * 72)
    print(f"  {'Timestamp':<22} {'Item':<20} {'Amount':>8} {'Unit':<8} Note")
    print("  " + "-" * 68)
    for e in entries:
        ts = e["timestamp"][:19].replace("T", " ")
        amount_str = f"+{e['amount']:.2f}" if e["amount"] < 0 else f"-{e['amount']:.2f}"
        note = e.get("note", "")[:20]
        print(f"  {ts:<22} {e['item_name']:<20} {amount_str:>8} "
              f"{e['unit']:<8} {note}")
    print()
    print(f"  Showing {len(entries)} record(s).")
    print()


def show_report():
    inv = load_inventory()
    usage = load_usage()

    print()
    print("=" * 72)
    print(f"  {'USAGE REPORT':^68}")
    print("=" * 72)

    # Total value
    total_value = sum(i["quantity"] * i["price"] for i in inv.values())
    print(f"\n  Total inventory value: ${total_value:.2f}")

    # Per-item usage summary
    consumed: dict[str, float] = {}
    restocked: dict[str, float] = {}
    for e in usage:
        key = e["item_key"]
        if e["amount"] < 0:  # restock
            restocked[key] = restocked.get(key, 0) + abs(e["amount"])
        else:
            consumed[key] = consumed.get(key, 0) + e["amount"]

    if consumed:
        print(f"\n  {'Top Consumed Items':}")
        print(f"  {'Item':<25} {'Total Used':>12} {'Unit':<8}")
        print("  " + "-" * 48)
        sorted_consumed = sorted(consumed.items(), key=lambda x: x[1], reverse=True)
        for key, total in sorted_consumed[:10]:
            item = inv.get(key, {})
            unit = item.get("unit", "")
            display = item.get("name", key)
            print(f"  {display:<25} {total:>12.2f} {unit:<8}")

    if restocked:
        print(f"\n  {'Top Restocked Items':}")
        print(f"  {'Item':<25} {'Total Added':>12} {'Unit':<8}")
        print("  " + "-" * 48)
        for key, total in sorted(restocked.items(), key=lambda x: x[1], reverse=True)[:10]:
            item = inv.get(key, {})
            unit = item.get("unit", "")
            display = item.get("name", key)
            print(f"  {display:<25} {total:>12.2f} {unit:<8}")

    low = [i for i in inv.values() if i["quantity"] <= i["low_stock_threshold"]]
    if low:
        print(f"\n  Low Stock Items ({len(low)}):")
        for item in low:
            print(f"    - {item['name']}: {item['quantity']} {item['unit']} "
                  f"(threshold: {item['low_stock_threshold']})")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE_TEXT = """
Inventory Tracker with Usage

Usage:
  inventory_tracker.py add <name> <qty> <unit> [category] [threshold] [price] [distributor] [warehouse]
  inventory_tracker.py update <name> [--qty=N] [--unit=U] [--cat=C] [--threshold=T] [--price=P] [--distributor=D] [--warehouse=W]
  inventory_tracker.py use <name> <amount> [note]
  inventory_tracker.py restock <name> <amount> [note]
  inventory_tracker.py remove <name>
  inventory_tracker.py list [category]
  inventory_tracker.py history [name] [--limit=N]
  inventory_tracker.py report

Examples:
  python inventory_tracker.py add "Coffee Beans" 500 grams beverages 100 12.99
  python inventory_tracker.py use "Coffee Beans" 30 "Morning brew"
  python inventory_tracker.py restock "Coffee Beans" 250 "New bag"
  python inventory_tracker.py list
  python inventory_tracker.py history "Coffee Beans"
  python inventory_tracker.py report
"""


def parse_kwarg(args: list[str], flag: str) -> Optional[str]:
    prefix = f"--{flag}="
    for a in args:
        if a.startswith(prefix):
            return a[len(prefix):]
    return None


def main():
    args = sys.argv[1:]
    if not args:
        print(USAGE_TEXT)
        return

    cmd = args[0].lower()

    if cmd == "add":
        if len(args) < 4:
            print("  Usage: add <name> <qty> <unit> [category] [threshold] [price] [distributor] [warehouse]")
            return
        name = args[1]
        qty = float(args[2])
        unit = args[3]
        category = args[4] if len(args) > 4 else "general"
        threshold = float(args[5]) if len(args) > 5 else 5.0
        price = float(args[6]) if len(args) > 6 else 0.0
        distributor = args[7] if len(args) > 7 else ""
        warehouse = args[8] if len(args) > 8 else ""
        add_item(name, qty, unit, category, threshold, price, distributor, warehouse)

    elif cmd == "update":
        if len(args) < 2:
            print("  Usage: update <name> [--qty=N] [--unit=U] [--cat=C] [--threshold=T] [--price=P] [--distributor=D] [--warehouse=W]")
            return
        name = args[1]
        qty_s = parse_kwarg(args[2:], "qty")
        unit_s = parse_kwarg(args[2:], "unit")
        cat_s = parse_kwarg(args[2:], "cat")
        thr_s = parse_kwarg(args[2:], "threshold")
        price_s = parse_kwarg(args[2:], "price")
        dist_s = parse_kwarg(args[2:], "distributor")
        wh_s = parse_kwarg(args[2:], "warehouse")
        update_item(
            name,
            quantity=float(qty_s) if qty_s else None,
            unit=unit_s,
            category=cat_s,
            low_stock_threshold=float(thr_s) if thr_s else None,
            price=float(price_s) if price_s else None,
            distributor=dist_s,
            warehouse=wh_s,
        )

    elif cmd == "use":
        if len(args) < 3:
            print("  Usage: use <name> <amount> [note]")
            return
        name = args[1]
        amount = float(args[2])
        note = args[3] if len(args) > 3 else ""
        record_usage(name, amount, note)

    elif cmd == "restock":
        if len(args) < 3:
            print("  Usage: restock <name> <amount> [note]")
            return
        name = args[1]
        amount = float(args[2])
        note = args[3] if len(args) > 3 else ""
        restock(name, amount, note)

    elif cmd == "remove":
        if len(args) < 2:
            print("  Usage: remove <name>")
            return
        remove_item(args[1])

    elif cmd in ("list", "ls"):
        category = args[1] if len(args) > 1 else None
        show_inventory(category)

    elif cmd in ("history", "log"):
        name = None
        limit = 20
        remaining = args[1:]
        limit_s = parse_kwarg(remaining, "limit")
        if limit_s:
            limit = int(limit_s)
            remaining = [a for a in remaining if not a.startswith("--limit=")]
        if remaining:
            name = remaining[0]
        show_usage(name, limit)

    elif cmd == "report":
        show_report()

    elif cmd in ("help", "--help", "-h"):
        print(USAGE_TEXT)

    else:
        print(f"  Unknown command: '{cmd}'")
        print(USAGE_TEXT)


if __name__ == "__main__":
    main()
