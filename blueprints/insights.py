"""Count checks + order-plan summary for the Inventory tab.

``GET /api/insights`` -> {
    "count_checks": {"items": {item_key: check}, "warehouses": [...]},
    "orders":       per-DC order plan (integrations/order_plan.py) on the
                    configured usage basis -- the Inventory tab shows a chip
                    per warehouse; the full view is the Production Planner.
}
Read-only. Count-check tunables live under "count_check" and ordering rules
under "ordering" in data/planning_config.json (no deploy needed).
"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify

from core.errors import _log_exc

insights_bp = Blueprint("insights", __name__)


def build_insights() -> dict:
    from inventory_tracker import load_inventory, load_usage, load_inventory_audit
    from integrations.planning_config import load_planning_config
    from integrations.stock_insights import count_checks
    from integrations.order_plan import plan_orders
    from blueprints.pos import build_po_ledger
    cfg = load_planning_config()
    inv = load_inventory()
    ledger = build_po_ledger()
    checks = count_checks(inv, load_usage(), load_inventory_audit(0),
                          cfg.get("count_check") or {}, ledger=ledger)
    basis = (cfg.get("ordering") or {}).get("usage_basis") or "reps"
    plan = plan_orders(inv, cfg, basis, checks["items"], now=datetime.now())
    return {"ok": True, "count_checks": checks, "orders": plan}


@insights_bp.route("/api/insights")
def api_insights():
    try:
        return jsonify(build_insights())
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "insights")
        return jsonify({"ok": False, "error": "internal error"}), 500
