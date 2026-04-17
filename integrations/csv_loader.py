"""Shared CSV reader for distributor inventory feeds.

Supported columns (in any order, case-insensitive):
  - name           our local SKU name (exact match)
  - variety        e.g. "Plain", "Everything"
  - warehouse      e.g. "Ocala, FL"
  - quantity       current on-hand count (required)
  - price          price per unit (optional)
  - unit           unit of measure (optional)
  - distributor_sku the distributor's own SKU/product code (informational)

Either `name` OR (`variety` + `warehouse`) must be present so the row can be
matched to a local SKU. The distributor is supplied by the caller.
"""

import csv
from pathlib import Path
from typing import Iterator

from .base import SyncItem


def read_csv(path: Path, distributor: str) -> Iterator[SyncItem]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        normalized_fields = [h.lower().strip() for h in (reader.fieldnames or [])]
        reader.fieldnames = normalized_fields
        for raw in reader:
            row = {k.lower().strip(): (v or "").strip() for k, v in raw.items()}
            if not row.get("quantity"):
                continue
            try:
                qty = float(row["quantity"])
            except ValueError:
                continue
            price = None
            if row.get("price"):
                try:
                    price = float(row["price"])
                except ValueError:
                    pass
            yield SyncItem(
                quantity=qty,
                distributor=distributor,
                name=row.get("name") or None,
                variety=row.get("variety") or None,
                warehouse=row.get("warehouse") or None,
                unit=row.get("unit") or None,
                price=price,
                distributor_sku=row.get("distributor_sku") or None,
            )
