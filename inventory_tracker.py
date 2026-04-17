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
    return _load(INVENTORY_FILE)


def save_inventory(inv: dict):
    _save(INVENTORY_FILE, inv)


def load_usage() -> list:
    return _load(USAGE_FILE)


def save_usage(usage: list):
    _save(USAGE_FILE, usage)


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def add_item(name: str, quantity: float, unit: str, category: str = "general",
             low_stock_threshold: float = 5.0, price: float = 0.0,
             distributor: str = "", warehouse: str = ""):
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
        "added": datetime.now().isoformat(),
    }
    save_inventory(inv)
    print(f"  Added '{name}': {quantity} {unit}")


def update_item(name: str, quantity: Optional[float] = None,
                unit: Optional[str] = None, category: Optional[str] = None,
                low_stock_threshold: Optional[float] = None,
                price: Optional[float] = None,
                distributor: Optional[str] = None,
                warehouse: Optional[str] = None):
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
