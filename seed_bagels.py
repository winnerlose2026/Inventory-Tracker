#!/usr/bin/env python3
"""Seed the inventory with our unified bagel list from Cheney Brothers and US Foods.

Varieties carried: plain, everything, sesame, cinnamon raisin, whole wheat,
whole wheat everything, blueberry, egg, onion, asiago, jalapeno cheddar.

Each variety is stocked from BOTH distributors so the unified list shows the
same product side-by-side for price and stock comparison (22 SKUs total).

Usage:
    python seed_bagels.py          # add missing items, skip existing
    python seed_bagels.py --reset  # wipe inventory first, then seed
"""

import sys
from inventory_tracker import add_item, load_inventory, save_inventory


# (variety, Cheney price, US Foods price, Cheney qty, US Foods qty, threshold)
VARIETIES = [
    ("Plain",                   0.55, 0.53, 144, 144, 48),
    ("Everything",              0.58, 0.56, 144, 144, 48),
    ("Sesame",                  0.57, 0.55,  72,  72, 36),
    ("Cinnamon Raisin",         0.62, 0.60,  72,  72, 36),
    ("Whole Wheat",             0.58, 0.56,  72,  72, 36),
    ("Whole Wheat Everything",  0.64, 0.62,  36,  36, 24),
    ("Blueberry",               0.63, 0.60,  72,  72, 36),
    ("Egg",                     0.57, 0.55,  72,  72, 36),
    ("Onion",                   0.57, 0.55,  72,  72, 36),
    ("Asiago",                  0.66, 0.64,  36,  36, 24),
    ("Jalapeno Cheddar",        0.68, 0.66,  36,  36, 24),
]


def _build_bagels():
    bagels = []
    for variety, cb_price, usf_price, cb_qty, usf_qty, threshold in VARIETIES:
        bagels.append({
            "name": f"{variety} Bagel 4oz (Cheney)",
            "quantity": cb_qty, "unit": "each", "price": cb_price,
            "threshold": threshold, "distributor": "Cheney Brothers",
        })
        bagels.append({
            "name": f"{variety} Bagel 4oz (US Foods)",
            "quantity": usf_qty, "unit": "each", "price": usf_price,
            "threshold": threshold, "distributor": "US Foods",
        })
    return bagels


BAGELS = _build_bagels()


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

    cheney = sum(1 for b in BAGELS if b["distributor"] == "Cheney Brothers")
    usf = sum(1 for b in BAGELS if b["distributor"] == "US Foods")
    print()
    print(f"  Seed complete: {added} added, {skipped} already present.")
    print(f"  Varieties: {len(VARIETIES)}")
    print(f"  Cheney Brothers SKUs: {cheney}")
    print(f"  US Foods SKUs:        {usf}")
    print(f"  Total SKUs:           {cheney + usf}")


if __name__ == "__main__":
    seed(reset="--reset" in sys.argv)
