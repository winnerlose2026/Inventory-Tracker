#!/usr/bin/env python3
"""Inventory Tracker with Usage History"""

import json
import os
import sys
from datetime import datetime
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
    if _rollover_on_order(inv):
        # Promoted at least one pending order — persist both sides.
        usage = _load(USAGE_FILE)
        _append_rollover_usage(inv, usage)
        _save(INVENTORY_FILE, inv)
        _save(USAGE_FILE, usage)
    return inv


def save_inventory(inv: dict):
    _save(INVENTORY_FILE, inv)


def load_usage() -> list:
    return _load(USAGE_FILE)


def save_usage(usage: list):
    _save(USAGE_FILE, usage)


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


def _rollover_on_order(inv: dict) -> bool:
    """Promote on_order entries whose ETA has passed into quantity.
    Mutates `inv` in place. Returns True if any entry was promoted."""
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
            eta_str = entry.get("eta", "")
            try:
                eta = datetime.fromisoformat(eta_str)
            except (TypeError, ValueError):
                kept.append(entry)
                continue
            if eta > now:
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
                "eta": eta_str,
                "timestamp": now.isoformat(),
            })
            changed = True
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
     