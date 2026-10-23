"""Planning reference data (Phase 1 of the weekly production planner).

Single source for the planner\'s reference tables: warehouses (region,
transit, transfer pool), varieties (top-4), capacity, freezer cap, buffer
targets, and distributor priority. Ships sane DEFAULTS in code; an optional
data/planning_config.json on the service disk overrides any key WITHOUT a
deploy and fails safe to DEFAULTS on any error -- same pattern as rep_map.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

TOP4 = ["Plain", "Everything", "Sesame", "Cinnamon Raisin"]

_VARIETIES = [
    "Plain", "Everything", "Sesame", "Cinnamon Raisin", "Asiago", "Blueberry",
    "Egg", "Jalapeno Cheddar", "Onion", "Poppy Seed", "Whole Wheat",
    "Whole Wheat Everything",
]

_WAREHOUSES = [
    {"distributor": "US Foods", "warehouse": "Manassas, VA", "region": "Mid-Atlantic", "transit_days": 7, "transfer_group": None},
    {"distributor": "US Foods", "warehouse": "Zebulon, NC", "region": "Southeast", "transit_days": 7, "transfer_group": None, "min_po_pallets": 6},
    {"distributor": "US Foods", "warehouse": "La Mirada, CA", "region": "West", "transit_days": 7, "transfer_group": None, "min_po_pallets": 6},
    {"distributor": "US Foods", "warehouse": "Chicago, IL", "region": "Midwest", "transit_days": 7, "transfer_group": None, "min_po_pallets": 6},
    {"distributor": "US Foods", "warehouse": "Alcoa, TN", "region": "Southeast", "transit_days": 7, "transfer_group": None},
    {"distributor": "US Foods", "warehouse": "Houston, TX", "region": "South", "transit_days": 7, "transfer_group": None},
    {"distributor": "Cheney Brothers", "warehouse": "Riviera Beach, FL", "region": "Florida", "transit_days": 7, "transfer_group": "cheney-fl", "min_po_pallets": 6},
    {"distributor": "Cheney Brothers", "warehouse": "Ocala, FL", "region": "Florida", "transit_days": 7, "transfer_group": "cheney-fl", "min_po_pallets": 6},
    {"distributor": "Cheney Brothers", "warehouse": "Punta Gorda, FL", "region": "Florida", "transit_days": 7, "transfer_group": "cheney-fl"},
]

DEFAULTS = {
    "pallet_cs": 56,
    # Transit is an ASSUMPTION (no measured delivery dates yet) -- plan long.
    "transit_default_days": 7,
    # Internal finished-goods buffer ceiling: ~110 pallets of freezer.
    "freezer_pallet_cap": 110,
    "capacity": {
        "dependable_cs_per_day": 224,   # 4 pallets/day -- the rate we reliably hit
        # JD 2026-10-23: production caps at 6 pallets/day, Mon-Fri.
        "max_cs_per_day": 336,
        "production_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "weekend_surge": False,
        "expansion_levers": ["packaging-machine efficiency", "true night shift"],
    },
    "top4": TOP4,
    # Warehouse service level (cover to defend at each distributor warehouse).
    "warehouse_buffer_days": {"top4_floor": 7, "top4_target": 14, "other_floor": 5},
    # Target days of top-4 to build-ahead in H&H\'s own freezer (within cap).
    "internal_buffer_target_days_top4": 10,
    "distributor_priority": ["US Foods", "Cheney Brothers", "Chefs Warehouse"],
    "transfer_groups": {
        # Cheney FL warehouses POOL stock -- they transfer cases between each
        # other to avoid OOS, so the planner judges their cover at the POOL
        # level (a thin single warehouse may be coverable by a sibling).
        "cheney-fl": ["Riviera Beach, FL", "Ocala, FL", "Punta Gorda, FL"],
    },
    "warehouses": _WAREHOUSES,
    # PO minimums (JD 2026-10-23): 2 pallets everywhere; 6 at Riviera Beach,
    # La Mirada, Ocala, US Foods Chicago and Zebulon (per-warehouse
    # "min_po_pallets" above). Varieties mix on a pallet, so only the PO
    # total has to land on whole pallets.
    "min_po_pallets_default": 2,
    # Order planning (integrations/order_plan.py). Lead time is fixed at 30
    # days (JD 2026-10-23); usage "reps" = the weekly usage the warehouse
    # reps report, "counts" = what our count-to-count history shows.
    "ordering": {
        "lead_days": 30,
        "review_days": 7,
        "safety_days_top4": 14,
        "safety_days_other": 10,
        "line_multiple_cs": 8,
        "usage_basis": "reps",
        "horizon_days": 56,
        "exclude_categories": ["bagels-lto"],
    },
    "varieties": [{"name": v, "top4": v in TOP4, "case_size": 60} for v in _VARIETIES],
}

_CACHE = {"key": None, "data": None}


def _override_path() -> Path:
    return Path(os.environ.get("PLANNING_CONFIG_FILE", "data/planning_config.json"))


def _copy(o):
    return json.loads(json.dumps(o))


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_planning_config() -> dict:
    """DEFAULTS deep-merged with the optional on-disk override. Fail-safe."""
    path = _override_path()
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return _copy(DEFAULTS)
    if _CACHE["key"] == key and _CACHE["data"] is not None:
        return _copy(_CACHE["data"])
    cfg = _copy(DEFAULTS)
    try:
        over = json.loads(path.read_text())
        if isinstance(over, dict):
            cfg = _deep_merge(cfg, over)
    except Exception:  # noqa: BLE001 -- never let a bad override break callers
        cfg = _copy(DEFAULTS)
    _CACHE["key"] = key
    _CACHE["data"] = _copy(cfg)
    return _copy(cfg)


def is_top4(variety: str) -> bool:
    return (variety or "").strip() in set(load_planning_config().get("top4") or [])


def transfer_group_for(warehouse: str):
    w = (warehouse or "").strip()
    for wh in load_planning_config().get("warehouses") or []:
        if wh.get("warehouse") == w:
            return wh.get("transfer_group")
    return None


def pool_members(group: str) -> list:
    return list((load_planning_config().get("transfer_groups") or {}).get(group) or [])


def min_po_pallets_for(warehouse: str) -> int:
    cfg = load_planning_config()
    w = (warehouse or "").strip()
    for wh in cfg.get("warehouses") or []:
        if wh.get("warehouse") == w and wh.get("min_po_pallets"):
            return int(wh["min_po_pallets"])
    return int(cfg.get("min_po_pallets_default") or 2)


def transit_days_for(warehouse: str) -> int:
    cfg = load_planning_config()
    w = (warehouse or "").strip()
    for wh in cfg.get("warehouses") or []:
        if wh.get("warehouse") == w:
            return int(wh.get("transit_days") or cfg.get("transit_default_days") or 7)
    return int(cfg.get("transit_default_days") or 7)
