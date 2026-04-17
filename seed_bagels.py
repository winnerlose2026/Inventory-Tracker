#!/usr/bin/env python3
"""Seed the inventory with our unified bagel list from Cheney Brothers and US Foods.

Each SKU is listed once per distributor (so the same product carried by both
distributors appears as two distinct SKUs in the unified list).

Usage:
    python seed_bagels.py          # add missing items, skip existing
    python seed_bagels.py --reset  # wipe inventory first, then seed
"""

import sys
from inventory_tracker import add_item, load_inventory, save_inventory


BAGELS = [
    # --- Cheney Brothers ---
    {"name": "Plain Bagel 4oz (Cheney)",           "quantity": 144, "unit": "each", "price": 0.55, "threshold": 48, "distributor": "Cheney Brothers"},
    {"name": "Everything Bagel 4oz (Cheney)",      "quantity": 144, "unit": "each", "price": 0.58, "threshold": 48, "distributor": "Cheney Brothers"},
    {"name": "Sesame Seed Bagel 4oz (Cheney)",     "quantity": 72,  "unit": "each", "price": 0.57, "threshold": 36, "distributor": "Cheney Brothers"},
    {"name": "Cinnamon Raisin Bagel 4oz (Cheney)", "quantity": 72,  "unit": "each", "price": 0.60, "threshold": 36, "distributor": "Cheney Brothers"},
    {"name": "Poppy Seed Bagel 4oz (Cheney)",      "quantity": 72,  "unit": "each", "price": 0.57, "threshold": 36, "distributor": "Cheney Brothers"},
    {"name": "Asiago Cheese Bagel 4oz (Cheney)",   "quantity": 36,  "unit": "each", "price": 0.65, "threshold": 24, "distributor": "Cheney Brothers"},
    {"name": "Pumpernickel Bagel 4oz (Cheney)",    "quantity": 36,  "unit": "each", "price": 0.62, "threshold": 24, "distributor": "Cheney Brothers"},

    # --- US Foods ---
    {"name": "Plain Bagel 4oz (US Foods)",           "quantity": 144, "unit": "each", "price": 0.53, "threshold": 48, "distributor": "US Foods"},
    {"name": "Everything Bagel 4oz (US Foods)",      "quantity": 144, "unit": "each", "price": 0.56, "threshold": 48, "distributor": "US Foods"},
    {"name": "Sesame Seed Bagel 4oz (US Foods)",     "quantity": 72,  "unit": "each", "price": 0.55, "threshold": 36, "distributor": "US Foods"},
    {"name": "Onion Bagel 4oz (US Foods)",           "quantity": 72,  "unit": "each", "price": 0.57, "threshold": 36, "distributor": "US Foods"},
    {"name": "Blueberry Bagel 4oz (US Foods)",       "quantity": 72,  "unit": "each", "price": 0.60, "threshold": 36, "distributor": "US Foods"},
    {"name": "Whole Wheat Bagel 4oz (US Foods)",     "quantity": 72,  "unit": "each", "price": 0.58, "threshold": 36, "distributor": "US Foods"},
    {"name": "Mini Plain Bagel 1oz (US Foods)",      "quantity": 240, "unit": "each", "price": 0.24, "threshold": 60, "distributor": "US Foods"},
    {"name": "Mini Everything Bagel 1oz (US Foods)", "quantity": 120, "unit": "each", "price": 0.25, "threshold": 60, "distributor": "US Foods"},
]


def seed(reset: bool = False):
    if reset:
        print("  Resetting inventory...")
        save_inventory({})

    existing = load_inventory()
    added = 0
    skipped = 0
    for b in BAGELS:
        if b["name"].lower() in existing:
            skipped += 1
            continue
        add_item(
            name=b["name"],
            quantity=b["quantity"],
            unit=b["unit"],
            category="bagels",
            low_stock_threshold=b["threshold"],
            price=b["price"],
            distributor=b["distributor"],
        )
        added += 1

    print()
    print(f"  Seed complete: {added} added, {skipped} already present.")
    print(f"  Cheney Brothers SKUs: {sum(1 for b in BAGELS if b['distributor'] == 'Cheney Brothers')}")
    print(f"  US Foods SKUs:        {sum(1 for b in BAGELS if b['distributor'] == 'US Foods')}")


if __name__ == "__main__":
    seed(reset="--reset" in sys.argv)
