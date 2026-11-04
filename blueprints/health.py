"""Health blueprint — open, unauthenticated liveness probe.

First slice of the blueprint refactor. Deliberately has ZERO coupling to
app-level helpers, so it is the safe proof-of-pattern: a Flask Blueprint
defined here and registered on the app in app.py. The route's endpoint name
becomes ``health.healthz`` (blueprint-qualified), which is why app.py's
_OPEN_ENDPOINTS allowlist references ``health.healthz``.
"""
import os
import time
from pathlib import Path

from flask import Blueprint, jsonify

health_bp = Blueprint("health", __name__)


def _read_version() -> str:
    """Release version from the repo-root VERSION file ("unknown" if absent)."""
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


VERSION = _read_version()
# Render sets RENDER_GIT_COMMIT on every build; lets /healthz say exactly
# which commit is live so a release tag can be checked against production.
COMMIT = (os.environ.get("RENDER_GIT_COMMIT") or "")[:7] or None


@health_bp.route("/healthz")
def healthz():
    """Liveness probe for uptime monitors / load balancers. Touches no data."""
    return jsonify({
        "ok": True,
        "service": "inventory-tracker",
        "version": VERSION,
        "commit": COMMIT,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
