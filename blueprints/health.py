"""Health blueprint — open, unauthenticated liveness probe.

First slice of the blueprint refactor. Deliberately has ZERO coupling to
app-level helpers, so it is the safe proof-of-pattern: a Flask Blueprint
defined here and registered on the app in app.py. The route's endpoint name
becomes ``health.healthz`` (blueprint-qualified), which is why app.py's
_OPEN_ENDPOINTS allowlist references ``health.healthz``.
"""
import time

from flask import Blueprint, jsonify

health_bp = Blueprint("health", __name__)


@health_bp.route("/healthz")
def healthz():
    """Liveness probe for uptime monitors / load balancers. Touches no data."""
    return jsonify({
        "ok": True,
        "service": "inventory-tracker",
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
