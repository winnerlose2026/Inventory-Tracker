#!/usr/bin/env python3
"""Seed the inventory with our unified bagel list from Cheney Brothers and US Foods.

Varieties carried: plain, everything, sesame, poppy seed, cinnamon raisin,
whole wheat, whole wheat everything, blueberry, egg, onion, asiago,
jalapeno cheddar.

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

Case economics (set on every SKU so they sync through reports and exports):
  - Cheney Brothers case cost:  $26.50
  - US Foods case cost:         $27.00
  - Case size:                  60 bagels (5 dozen) across both distributors

Quantities, thresholds, and weekly_usage are all stored in CASES (unit="cs")
so they line up with PO parser output and case-priced reordering. Multiply
by case_size when you need a per-bagel figure.

12 varieties x 8 warehouses = 96 SKUs.

Usage:
    python seed_bagels.py          # add missing items, skip existing
    python seed_bagels.py --reset  # wipe inventory first, then seed
"""

import sys
from inventory_tracker import add_item, load_inventory, save_inventory


# Flat case cost per distributor. Both distributors ship 5 doz (60) per case.
CASE_COST = {"Cheney Brothers": 26.50, "US Foods": 27.00}
CASE_SIZE = 60

# Variety -> (weekly usage cs/wk, on-hand cases, low-stock threshold cases).
# Quantities are in CASES so they line up with PO parser output (which is
# also in CS) and the inventory unit ("cs"). Mechanical conversion of the
# old per-bagel numbers via /CASE_SIZE.
VARIETIES = [
    ("Plain",                   2.0, 2.4, 0.8),
    ("Everything",              1.7, 2.4, 0.8),
    ("Sesame",                  0.7, 1.2, 0.6),
    ("Poppy Seed",              0.7, 1.2, 0.6),
    ("Cinnamon Raisin",         0.6, 1.2, 0.6),
    ("Whole Wheat",             0.7, 1.2, 0.6),
    ("Whole Wheat Everything",  0.3, 0.6, 0.4),
    ("Blueberry",               0.6, 1.2, 0.6),
    ("Egg",                     0.5, 1.2, 0.6),
    ("Onion",                   0.5, 1.2, 0.6),
    ("Asiago",                  0.3, 0.6, 0.4),
    ("Jalapeno Cheddar",        0.3, 0.6, 0.4),
]

# Distributor -> [(warehouse label, short tag, stock multiplier)]
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
    for variety, weekly, base_qty, threshold in VARIETIES:
        for distributor, warehouses in WAREHOUSES.items():
            case_cost = CASE_COST[distributor]
            tag = DISTRIBUTOR_TAG[distributor]
            for warehouse_full, warehouse_short, mult in warehouses:
                bagels.append({
                    "name": f"{variety} Bagel 4oz [{tag} - {warehouse_short}]",
                    "quantity": round(base_qty * mult, 1),
                    "unit": "cs",
                    "price": case_cost,
                    "threshold": round(threshold, 1),
                    "distributor": distributor,
                    "warehouse": warehouse_full,
                    "case_cost": case_cost,
                    "case_size": CASE_SIZE,
                    "weekly_usage": round(weekly * mult, 2),
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
            case_cost=b["case_cost"],
            case_size=b["case_size"],
            weekly_usage=b["weekly_usage"],
        )
        added += 1

    cheney = sum(1 for b in BAGELS if b["distributor"] == "Cheney Brothers")
    usf = sum(1 for b in BAGELS if b["distributor"] == "US Foods")
    print()
    print(f"  Seed complete: {added} added, {skipped} already present.")
    print(f"  Varieties: {len(VARIETIES)}")
    print(f"  Case size: {CASE_SIZE} bagels (5 dozen)")
    print(f"  Case cost: Cheney ${CASE_COST['Cheney Brothers']:.2f}  "
          f"US Foods ${CASE_COST['US Foods']:.2f}")
    print(f"  Cheney Brothers SKUs: {cheney}")
    print(f"  US Foods SKUs:        {usf}")
    print(f"  Total SKUs:           {cheney + usf}")
    return {
        "added": added,
        "skipped": skipped,
        "total": cheney + usf,
        "cheney": cheney,
        "us_foods": usf,
        "reset": bool(reset),
    }


if __name__ == "__main__":
    seed(reset="--reset" in sys.argv)
