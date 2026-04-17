#!/usr/bin/env python3
"""Seed the inventory with our unified bagel list from Cheney Brothers and US Foods.

Varieties carried: plain, everything, sesame, cinnamon raisin, whole wheat,
whole wheat everything, blueberry, egg, onion, asiago, jalapeno cheddar.

Stock is tracked per warehouse (each distributor has multiple warehouses that
house our bagels):

  Cheney Brothers (3 FL warehouses)
    - Riviera Beach, FL
    - Ocala, FL
    - Punta Gorda, FL

  US Foods (5 warehouses)
    - Manassas, VA
    - Zebulon, NC
    - La Mirada, CA
    - Chicago, IL
    - Alcoa, TN

11 varieties × 8 warehouses = 88 SKUs.

Usage:
    python seed_bagels.py          # add missing items, skip existing
    python seed_bagels.py --reset  # wipe inventory first, then seed
"""

import sys
from inventory_tracker import add_item, load_inventory, save_inventory


# Variety -> (Cheney price, US Foods price, base qty, low-stock threshold)
VARIETIES = [
    ("Plain",                   0.55, 0.53, 144, 48),
    ("Everything",              0.58, 0.56, 144, 48),
    ("Sesame",                  0.57, 0.55,  72, 36),
    ("Cinnamon Raisin",         0.62, 0.60,  72, 36),
    ("Whole Wheat",             0.58, 0.56,  72, 36),
    ("Whole Wheat Everything",  0.64, 0.62,  36, 24),
    ("Blueberry",               0.63, 0.60,  72, 36),
    ("Egg",                     0.57, 0.55,  72, 36),
    ("Onion",                   0.57, 0.55,  72, 36),
    ("Asiago",                  0.66, 0.64,  36, 24),
    ("Jalapeno Cheddar",        0.68, 0.66,  36, 24),
]

# Distributor -> [(warehouse label, short tag, stock multiplier)]
# Multipliers roughly reflect relative throughput so each warehouse has its
# own on-hand count rather than identical numbers.
WAREHOUSES = {
    "Cheney Brothers": [
        ("Riviera Beach, FL", "Riviera Beach", 1.0),
        ("Ocala, FL",         "Ocala",         1.2),  # largest Cheney DC
        ("Punta Gorda, FL",   "Punta Gorda",   0.7),
    ],
    "US Foods": [
        ("Manassas, VA",      "Manassas",      1.0),
        ("Zebulon, NC",       "Zebulon",       0.9),
        ("La Mirada, CA",     "La Mirada",     1.1),
        ("Chicago, IL",       "Chicago",       1.3),  # largest USF DC
        ("Alcoa, TN",         "Alcoa",         0.8),
    ],
}

DISTRIBUTOR_TAG = {"Cheney Brothers": "CB", "US Foods": "USF"}


def _build_bagels():
    bagels = []
    for variety, cb_price, usf_price, base_qty, threshold in VARIETIES:
        for distributor, warehouses in WAREHOUSES.items():
            price = cb_price if distributor == "Cheney Brothers" else usf_price
            tag = DISTRIBUTOR_TAG[distributor]
            for warehouse_full, warehouse_short, mult in warehouses:
                bagels.append({
                    "name": f"{variety} Bagel 4oz [{tag} - {warehouse_short}]",
                    "quantity": int(round(base_qty * mult)),
                    "unit": "each",
                    "price": price,
                    "threshold": threshold,
                    "distributor": distributor,
                    "warehouse": warehouse_full,
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
            warehouse=b["warehouse"],
        )
        added += 1

    cheney = sum(1 for b in BAGELS if b["distributor"] == "Cheney Brothers")
    usf = sum(1 for b in BAGELS if b["distributor"] == "US Foods")
    print()
    print(f"  Seed complete: {added} added, {skipped} already present.")
    print(f"  Varieties: {len(VARIETIES)}")
    print(f"  Warehouses: "
          f"{len(WAREHOUSES['Cheney Brothers'])} Cheney + "
          f"{len(WAREHOUSES['US Foods'])} US Foods")
    print(f"  Cheney Brothers SKUs: {cheney}")
    print(f"  US Foods SKUs:        {usf}")
    print(f"  Total SKUs:           {cheney + usf}")


if __name__ == "__main__":
    seed(reset="--reset" in sys.argv)
