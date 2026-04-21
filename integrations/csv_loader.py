"""Shared CSV reader for distributor inventory feeds.

Supported columns (in any order, case-insensitive):
  - name           our local SKU name (exact match)
  - variety        e.g. "Plain", "Everything"
  - warehouse      e.g. "Ocala, FL"
  - quantity       current on-hand count (required)
  - price          price per unit (optional)
  - unit           unit of measure (optional)
  - distributor_sku the distributor's own SKU/product code (informational)
  - case_cost      cost per case (optional)
  - case_size      units per case (optional, e.g. 60 = 5 dozen)
  - weekly_usage   average units consumed per week (optional)

Either `name` OR (`variety` + `warehouse`) must be present so the row can be
matched to a local SKU. The distributor is supplied by the caller.
"""

import csv
from pathlib import Path
from typing import Iterator

from .base import SyncItem


def _opt_float(v: str):
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _opt_int(v: str):
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


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
            yield SyncItem(
                quantity=qty,
                distributor=distributor,
                name=row.get("name") or None,
                variety=row.get("variety") or None,
                warehouse=row.get("warehouse") or None,
                unit=row.get("unit") or None,
                price=_opt_float(row.get("price", "")),
                distributor_sku=row.get("distributor_sku") or None,
                case_cost=_opt_float(row.get("case_cost", "")),
                case_size=_opt_int(row.get("case_size", "")),
                weekly_usage=_opt_float(row.get("weekly_usage", "")),
            )
