"""Count checks + reorder suggestions (integrations/stock_insights.py).

``GET /api/insights`` -> {
    "count_checks": {"items": {item_key: check}, "warehouses": [...]},
    "reorder":      {"warehouses": [...], "excluded_varieties": [...]},
}
Read-only. Tunables live under "reorder" / "count_check" in
data/planning_config.json (no deploy needed), e.g.
    {"reorder": {"safety_days_top4": 21, "review_days": 14}}
"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify

from core.errors import _log_exc

insights_bp = Blueprint("insights", __name__)


def build_insights() -> dict:
    from inventory_tracker import load_inventory, load_usage, load_inventory_audit
    from integrations.planning_config import load_planning_config
    from integrations.stock_insights import count_checks, reorder_suggestions
    from blueprints.pos import build_po_ledger
    cfg = load_planning_config()
    inv = load_inventory()
    ledger = build_po_ledger()
    checks = count_checks(inv, load_usage(), load_inventory_audit(0),
                          cfg.get("count_check") or {}, ledger=ledger)
    reorder = reorder_suggestions(inv, ledger, cfg.get("reorder") or {},
                                  now=datetime.now(), top4=set(cfg.get("top4") or []),
                                  count_items=checks["items"])
    return {"ok": True, "count_checks": checks, "reorder": reorder}


@insights_bp.route("/api/insights")
def api_insights():
    try:
        return jsonify(build_insights())
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "insights")
        return jsonify({"ok": False, "error": "internal error"}), 500
