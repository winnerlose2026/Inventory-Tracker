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
