"""Authentication helpers shared across the app and blueprints.

Session-based login for the browser, INVENTORY_API_TOKEN header for cron jobs
and scripts. Kept free of any app/blueprint import so anything can depend on it.
"""
import os
import secrets

from flask import request, session

# Endpoints reachable WITHOUT authentication: the login flow, static assets, the
# /api/auth/check probe the login page calls, and the Graph webhook (which
# authenticates via its own clientState, not our token). Blueprint routes use
# blueprint-qualified endpoint names (e.g. "health.healthz").
_OPEN_ENDPOINTS = {
    "login", "logout", "static", "api_auth_check",
    "graph_webhook_notifications", "health.healthz",
}


def _user_logged_in() -> bool:
    return bool(session.get("user"))


def _has_valid_api_token() -> bool:
    expected = os.environ.get("INVENTORY_API_TOKEN", "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Inventory-Token") or "").strip()
    return bool(got) and secrets.compare_digest(got, expected)


def _is_authenticated() -> bool:
    return _user_logged_in() or _has_valid_api_token()
