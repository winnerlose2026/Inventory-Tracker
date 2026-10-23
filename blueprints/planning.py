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


@planning_bp.route("/api/planning/plan")
def api_planning_plan():
    """Order plan -> production load -> bake schedule, plus the reps-vs-counts
    usage comparison. ?basis=reps (default: the configured basis) | counts.
    Read-only; gated by the auth hook."""
    from datetime import datetime
    from flask import request
    from core.errors import _log_exc
    try:
        from inventory_tracker import (load_inventory, load_usage, load_sales,
                                       load_inventory_audit)
        from integrations.production_planner import build_production_guide
        from integrations.demand_model import warehouse_demand
        from integrations.stock_insights import count_checks
        from integrations.order_plan import (plan_orders, production_load,
                                             daily_schedule, scenario_summary)
        from integrations.usage_compare import backtest
        from blueprints.pos import build_po_ledger
        cfg = load_planning_config()
        default_basis = (cfg.get("ordering") or {}).get("usage_basis") or "reps"
        basis = (request.args.get("basis") or default_basis).strip().lower()
        if basis not in ("reps", "counts"):
            basis = default_basis
        now = datetime.now()
        inv = load_inventory()
        usage = load_usage()
        audit = load_inventory_audit(0)
        ledger = build_po_ledger()
        checks = count_checks(inv, usage, audit, cfg.get("count_check") or {}, ledger=ledger)
        plans = {b: plan_orders(inv, cfg, b, checks["items"], now=now)
                 for b in ("reps", "counts")}
        guide = build_production_guide(inv=inv, ledger=ledger,
                                       demand_rows=warehouse_demand(inv), config=cfg,
                                       now=now)
        queue = guide.get("production_queue") or []
        loads = {b: production_load(queue, plans[b], cfg, ledger, now=now)
                 for b in plans}
        return jsonify({
            "ok": True, "basis": basis, "default_basis": default_basis,
            "orders": plans[basis],
            "load": loads[basis],
            "schedule": daily_schedule(queue, plans[basis], cfg, ledger, now=now),
            "compare": {
                "backtest": backtest(inv, usage, audit),
                "scenarios": {b: scenario_summary(plans[b]) for b in plans},
                "load_totals": {b: [{"week_of": w["week_of"], "total_cs": w["total_cs"],
                                     "pallets": w["pallets"]}
                                    for w in loads[b]["weeks"]] for b in loads},
            },
        })
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "planning plan")
        return jsonify({"ok": False, "error": "internal error"}), 500
