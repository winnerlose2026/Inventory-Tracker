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
    "webhooks.graph_webhook_notifications", "health.healthz",
}


def _user_logged_in() -> bool:
    return bool(session.get("user"))


def _has_valid_api_token() -> bool:
    expected = os.environ.get("INVENTORY_API_TOKEN", "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Inventory-Token") or "").strip()
    # bytes: compare_digest raises TypeError on non-ASCII str (a 500 from the
    # auth gate instead of a 401).
    return bool(got) and secrets.compare_digest(got.encode("utf-8"),
                                                expected.encode("utf-8"))


def _is_authenticated() -> bool:
    return _user_logged_in() or _has_valid_api_token()


# A second, LIMITED token for the Lineage PO delivery check, which runs as a
# cloud scheduled task (no laptop, so no access to the full token's file).
# It opens only these routes -- read the Quote and Schedule threads, post
# delivery events (each verified against the mailbox), and trigger the
# server-written follow-ups to the fixed Lineage list. Nothing else: no
# /api/email/send, no inventory writes, no other reads.
DELIVERY_TOKEN_ENDPOINTS = {
    "delivery.api_delivery_list",
    "delivery.api_delivery_sync_threads",
    "delivery.api_delivery_events",
    "delivery.api_delivery_send_followups",
}


def _has_valid_delivery_token() -> bool:
    expected = os.environ.get("DELIVERY_API_TOKEN", "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Inventory-Token") or "").strip()
    return bool(got) and secrets.compare_digest(got.encode("utf-8"),
                                                expected.encode("utf-8"))
