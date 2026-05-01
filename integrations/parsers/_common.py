"""Shared CSV helpers for SFTP inbox parsers."""

import csv
import io
from typing import Iterator

# Header aliases. Values are lowercased; the loader lowercases incoming
# headers before lookup. Add more aliases here when new distributor
# exports surface.
HEADER_ALIASES: dict[str, list[str]] = {
    "variety":         ["variety", "item description", "description",
                        "product description", "item name", "product"],
    "warehouse":       ["warehouse", "dc", "ship from", "ship-from",
                        "ship from dc", "warehouse name", "dc name",
                        "location", "branch"],
    "warehouse_code":  ["dc code", "warehouse code", "branch code",
                        "ship from code"],
    "distributor_sku": ["distributor_sku", "item #", "item number",
                        "sku", "product code", "item code"],
    "quantity":        ["quantity", "qty", "on hand", "on_hand",
                        "cases on hand", "cases", "case qty",
                        "shipped qty", "qty shipped"],
    "unit":            ["unit", "uom", "unit of measure", "pack"],
    "case_size":       ["case_size", "case size", "pack size",
                        "units per case"],
    "case_cost":       ["case_cost", "case cost", "cost", "unit cost",
                        "extended unit cost"],
    "price":           ["price", "unit price"],
    "extended_cost":   ["extended cost", "ext cost", "line total",
                        "amount"],
    "po_number":       ["po", "po #", "po number", "purchase order",
                        "purchase order #", "purchase order number",
                        "order #", "order number"],
    "po_revision":     ["po revision", "po rev", "revision",
                        "rev"],
    "order_date":      ["order date", "ship date", "invoice date",
                        "date", "delivery date"],
    "weekly_usage":    ["weekly_usage", "weekly usage", "avg weekly",
                        "4wk avg", "8wk avg", "13wk avg",
                        "moving average"],
    "as_of":           ["as_of", "as of", "snapshot timestamp",
                        "timestamp", "snapshot"],
}


def _norm_header(h: str) -> str:
    return (h or "").strip().lower().replace("﻿", "")


def _resolve(row: dict, key: str) -> str:
    # Try the canonical key first, then any registered aliases. This
    # guards against alias maps that document friendly variants but
    # forget to include the canonical name itself.
    candidates = [key, *HEADER_ALIASES.get(key, [])]
    seen: set[str] = set()
    for a in candidates:
        if a in seen:
            continue
        seen.add(a)
        if a in row:
            v = row[a]
            if v:
                return v.strip()
    return ""


def iter_rows(content: bytes) -> Iterator[dict]:
    """Yield dicts of {normalized_header: value_str} from a CSV blob.

    Tries UTF-8, falls back to latin-1 (cPanel exports sometimes go
    through Excel and pick up a BOM or cp1252 punctuation).
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        if raw is None:
            continue
        yield {_norm_header(k): (v or "").strip() for k, v in raw.items() if k}


def opt_float(s: str) -> float | None:
    if not s:
        return None
    s = s.replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def opt_int(s: str) -> int | None:
    f = opt_float(s)
    return int(f) if f is not None else None


# Map distributor-specific DC codes / city names to our canonical
# "City, ST" warehouse form used in the seed.
USF_DC_ALIASES = {
    # USF DC codes seen in our data
    "5o": "Manassas, VA",
    "2125": "Manassas, VA",
    "5g": "Tampa, FL",         # not currently a stocking DC for us
    "4c": "Phoenix, AZ",
    "manassas": "Manassas, VA",
    "zebulon": "Zebulon, NC",
    "la mirada": "La Mirada, CA",
    "chicago": "Chicago, IL",
    "alcoa": "Alcoa, TN",
}

CHENEY_DC_ALIASES = {
    "riviera": "Riviera Beach, FL",
    "riviera beach": "Riviera Beach, FL",
    "ocala": "Ocala, FL",
    "punta gorda": "Punta Gorda, FL",
}


def canonical_warehouse(distributor: str, raw: str, raw_code: str = "") -> str:
    """Normalize a distributor-supplied location to our canonical form.

    Tries the alias maps first, then falls back to the raw value if
    it already looks like 'City, ST'. Returns empty string if it can't
    figure it out — caller decides whether to skip the row.
    """
    aliases = USF_DC_ALIASES if distributor == "US Foods" else CHENEY_DC_ALIASES
    for candidate in (raw_code.lower().strip(), raw.lower().strip()):
        if not candidate:
            continue
        if candidate in aliases:
            return aliases[candidate]
        # Strip ", ST" and try again
        head = candidate.split(",", 1)[0].strip()
        if head in aliases:
            return aliases[head]
    # Already in "City, ST" form?
    if raw and "," in raw and len(raw.split(",", 1)[1].strip()) <= 3:
        return raw.strip()
    return ""


# Variety normalization: USF/Cheney often use abbreviations or extra
# words ("Plain Bagel 4oz", "BAGEL PLAIN 4 OZ FROZEN"). Map the most
# common shapes back to our canonical seed varieties.
_KNOWN_VARIETIES = [
    "Whole Wheat Everything", "Whole Wheat",
    "Cinnamon Raisin", "Jalapeno Cheddar",
    "Plain", "Everything", "Sesame", "Poppy Seed",
    "Blueberry", "Egg", "Onion", "Asiago",
]


def canonical_variety(raw: str) -> str:
    if not raw:
        return ""
    s = raw.lower()
    # Compound varieties first so "Whole Wheat Everything" doesn't
    # match "Everything" alone.
    for v in _KNOWN_VARIETIES:
        if v.lower() in s:
            return v
    # Common abbreviations
    if "cin rais" in s or "cinn rais" in s:
        return "Cinnamon Raisin"
    if "jal ched" in s or "jalapeno ched" in s:
        return "Jalapeno Cheddar"
    if "ww everything" in s or "wweverything" in s:
        return "Whole Wheat Everything"
    if "ww" in s or "whole wht" in s:
        return "Whole Wheat"
    return ""


def normalize_to_cases(qty: float, source_unit: str,
                      case_size: int | None) -> tuple[float, str]:
    """Convert a (qty, unit) reading into cases when our SKU is
    tracked in ``cs``.

    Returns (qty_in_cases, "cs") when a conversion is possible. If we
    can't convert, returns the input unchanged so the caller can decide
    whether to drop the row or pass it through.

    Cases pass through unchanged. Eaches divide by case_size. Anything
    else is left as-is (caller surfaces a warning).
    """
    u = (source_unit or "").strip().lower()
    if u in ("cs", "case", "cases", "ca"):
        return qty, "cs"
    if u in ("each", "ea", "eaches", "unit", "units", ""):
        if case_size and case_size > 0:
            # Round to 2 decimals — fractional cases are valid (we'll
            # render them as e.g. 2.4 cs in the UI). Inventory writes
            # round on display, not here.
            return round(qty / case_size, 2), "cs"
        # Unknown case_size: keep eaches; SKU resolver may still match
        # but the apply path will store the raw number.
        return qty, "each"
    # lb, oz, etc. — out of scope. Leave as-is.
    return qty, u

