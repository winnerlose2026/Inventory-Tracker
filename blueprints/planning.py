"""Planning blueprint -- reference config for the weekly production planner
(Phase 1). Read-only for now; planner compute endpoints land here next."""
from flask import Blueprint, jsonify

from integrations.planning_config import load_planning_config

planning_bp = Blueprint("planning", __name__)


@planning_bp.route("/api/planning/config")
def api_planning_config():
    """Resolved planning reference data: warehouses (region, transit,
    transfer pool), varieties/top-4, capacity, freezer cap, buffer targets,
    and distributor priority. DEFAULTS in code, overridable on disk via
    data/planning_config.json. Read-only; gated by the global auth hook.
    """
    return jsonify({"ok": True, "config": load_planning_config()})


@planning_bp.route("/api/planning/demand")
def api_planning_demand():
    """Unified demand: per warehouse-SKU direct run-rate (cases/day, freshness-
    weighted), the same aggregated by transfer pool, and the Toast leading-
    indicator trend. Read-only; gated by the auth hook."""
    from inventory_tracker import load_inventory, load_sales
    from integrations.demand_model import (
        warehouse_demand, pool_demand, toast_demand_trend,
    )
    wd = warehouse_demand(load_inventory())
    return jsonify({
        "ok": True,
        "warehouse_demand": wd,
        "pool_demand": pool_demand(wd),
        "toast_trend": toast_demand_trend(load_sales() or []),
        "stale_count": sum(1 for r in wd if r.get("stale")),
        "sku_count": len(wd),
    })


@planning_bp.route("/api/planning/guide")
def api_planning_guide():
    """The weekly production guide -- depletion, incoming-net, produce-by,
    priority (top-4 & US Foods/Cheney first), capacity rollup, build-ahead,
    and the Toast note. Combines config + PO ledger + demand + inventory.
    Read-only."""
    from inventory_tracker import load_inventory, load_sales
    from integrations.production_planner import build_production_guide
    from integrations.demand_model import warehouse_demand, toast_demand_trend
    from blueprints.pos import build_po_ledger
    cfg = load_planning_config()
    inv = load_inventory()
    guide = build_production_guide(
        inv=inv,
        ledger=build_po_ledger(),
        demand_rows=warehouse_demand(inv),
        config=cfg,
        toast_trend=toast_demand_trend(load_sales() or []),
    )
    return jsonify({"ok": True, **guide})
