#!/usr/bin/env python3
"""Inventory Tracker - Flask Web GUI"""

import io
import os
import secrets
from datetime import datetime, timedelta
from flask import (
    Flask, jsonify, request, render_template, send_file, make_response,
    session, redirect, url_for,
)
from inventory_tracker import (
    load_inventory, save_inventory, load_usage, save_usage,
    add_item, update_item, record_usage, restock, remove_item,
    reverse_usage,
)

app = Flask(__name__)

# Blueprints (incremental refactor of this monolith — see REFACTOR_PLAN.md).
# Imported after `app` exists; blueprint modules must not import this module.
from blueprints.health import health_bp  # noqa: E402
from blueprints.webhooks import webhooks_bp  # noqa: E402
from blueprints.freight import freight_bp  # noqa: E402
from blueprints.production import production_bp  # noqa: E402
from blueprints.pos import pos_bp  # noqa: E402
from blueprints.inventory import inventory_bp  # noqa: E402
from blueprints.admin import admin_bp  # noqa: E402
from blueprints.email import email_bp  # noqa: E402
from blueprints.reporting import reporting_bp  # noqa: E402
app.register_blueprint(health_bp)
app.register_blueprint(webhooks_bp)
app.register_blueprint(freight_bp)
app.register_blueprint(production_bp)
app.register_blueprint(pos_bp)
app.register_blueprint(inventory_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(email_bp)
app.register_blueprint(reporting_bp)

# Session config — 30-day signed cookies. SECRET_KEY should come from Render's
# environment (otherwise sessions reset on every redeploy when the random
# fallback regenerates).
app.config["SECRET_KEY"] = (
    os.environ.get("FLASK_SECRET_KEY")
    or os.environ.get("SECRET_KEY")
    or secrets.token_hex(32)
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# HTTPS-only cookie in production; on localhost dev we'd lock ourselves out.
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("FLASK_ENV", "").lower() != "development"
)
app.permanent_session_lifetime = timedelta(days=30)


# ---------------------------------------------------------------------------
# CORS — lets a Shopify-hosted page (or any allowed origin) call /api/*.
# Set ALLOWED_ORIGINS to a comma-separated list, e.g.
#   ALLOWED_ORIGINS=https://your-store.myshopify.com,https://your-store.com
# Leave unset during local development.
# ---------------------------------------------------------------------------

def _allowed_origins() -> list[str]:
    raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
    return [o.strip() for o in raw.split(",") if o.strip()]


def _origin_allowed(origin: str) -> bool:
    if not origin:
        return False
    allowed = _allowed_origins()
    if not allowed:
        return False
    if "*" in allowed:
        return True
    return origin in allowed


@app.after_request
def _apply_cors(response):
    origin = request.headers.get("Origin", "")
    if _origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Inventory-Token"
        response.headers["Access-Control-Max-Age"] = "600"
    return response


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def _cors_preflight(_any):
    # Short-circuit the preflight; _apply_cors adds the headers.
    return make_response("", 204)


# ---------------------------------------------------------------------------
# Auth — session-based login for the browser, INVENTORY_API_TOKEN header for
# cron jobs and scripts. A write request is authorised if EITHER:
#   1. The browser session has session["user"] set (humans), OR
#   2. The request carries the right X-Inventory-Token header (cron/scripts).
# Both reads and writes require auth: a browser session (humans) or the
# X-Inventory-Token header (cron/scripts). The only open endpoints are the
# login flow, static files, the /api/auth/check probe, and the Graph webhook
# (which authenticates via its own clientState, not our token).
# ---------------------------------------------------------------------------

# Shared helpers live in core/ now (incremental refactor — see REFACTOR_PLAN.md)
# so blueprints can import them without importing this module.
from core.auth import (  # noqa: E402
    _user_logged_in, _has_valid_api_token, _is_authenticated, _OPEN_ENDPOINTS,
)
from core.errors import _log_exc, _safe_err  # noqa: E402


# Shared cache + outbound-host helpers live in core/ now (refactor — see
# REFACTOR_PLAN.md). _AGG_CACHE is mutated by item assignment, never rebound.
from core.cache import _AGG_CACHE, _data_sig  # noqa: E402
from core.http import _TRUSTED_OUTBOUND_HOSTS  # noqa: E402
from core.util import _norm_po_key  # noqa: E402


# _VALIDATION_TOKEN_CHARS moved to blueprints/webhooks.py


# Endpoints reachable without authentication: the login flow, static assets,
# the auth-status probe the login page calls, and the Graph webhook (which
# authenticates via its own clientState, since Graph won't send our token).
# (_OPEN_ENDPOINTS is imported from core.auth above.)


@app.before_request
def _gate_requests():
    # CORS preflight is always allowed.
    if request.method == "OPTIONS":
        return
    if request.endpoint in _OPEN_ENDPOINTS:
        return
    # Everything else -- pages plus /api/* reads and writes -- requires a
    # logged-in session or a valid API token.
    if _is_authenticated():
        return
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    # Unauthenticated browser hitting an HTML page -> the login screen,
    # preserving the intended destination.
    nxt = request.full_path if request.query_string else request.path
    return redirect(url_for("login", next=nxt))


@app.route("/api/auth/check")
def api_auth_check():
    """Tell the page who's logged in (or whether the API token is valid)."""
    if _user_logged_in():
        return jsonify({
            "required": True,
            "authorized": True,
            "user": session.get("user"),
            "auth_type": "session",
        })
    if _has_valid_api_token():
        return jsonify({
            "required": True,
            "authorized": True,
            "user": None,
            "auth_type": "token",
        })
    return jsonify({"required": True, "authorized": False})


# ---------------------------------------------------------------------------
# Login throttling: in-memory per-IP failed-attempt lockout (per gunicorn
# worker; enough to blunt password brute force on a small internal tool).
# ---------------------------------------------------------------------------
_LOGIN_FAILS: dict = {}        # ip -> [monotonic timestamps of recent failures]
_LOGIN_WINDOW_S = 900          # 15-minute sliding window
_LOGIN_MAX_FAILS = 8           # lock after this many failures within the window


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "?"


def _login_locked(ip: str) -> bool:
    import time as _t
    now = _t.monotonic()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _LOGIN_WINDOW_S]
    _LOGIN_FAILS[ip] = fails
    return len(fails) >= _LOGIN_MAX_FAILS


def _login_record_fail(ip: str) -> None:
    import time as _t
    _LOGIN_FAILS.setdefault(ip, []).append(_t.monotonic())


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    next_url = request.args.get("next") or request.form.get("next") or "/"
    # Same-site redirects only. Inline the canonical Flask open-redirect guard
    # (resolve against this host; require the host to match) so the check
    # dominates the redirect sink in this function (CodeQL py/url-redirection).
    from urllib.parse import urlparse as _up_r, urljoin as _uj_r
    if (next_url.startswith("//") or "\\" in next_url
            or "\n" in next_url or "\r" in next_url or "\t" in next_url
            or _up_r(_uj_r(request.host_url, next_url)).netloc
               != _up_r(request.host_url).netloc):
        next_url = "/"

    if request.method == "POST":
        ip = _client_ip()
        if _login_locked(ip):
            return render_template(
                "login.html",
                error="Too many failed attempts. Please wait a few minutes and try again.",
                next_url=next_url,
            ), 429
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        # Allowed users come from INVENTORY_USERNAMES (comma-separated) with
        # backward-compat fallback to the legacy single INVENTORY_USERNAME.
        # All comparisons are case-insensitive so "jd" / "JD" / "Jd" all match.
        users_raw = (os.environ.get("INVENTORY_USERNAMES")
                     or os.environ.get("INVENTORY_USERNAME", ""))
        allowed = {u.strip().lower()
                   for u in users_raw.split(",") if u.strip()}
        expected_pass = os.environ.get("INVENTORY_PASSWORD", "")

        # Both the user list and password must be set for login to be
        # possible. If either is missing, the operator hasn't finished
        # configuration — fail closed.
        if allowed and expected_pass \
                and username.lower() in allowed \
                and secrets.compare_digest(password, expected_pass):
            _LOGIN_FAILS.pop(ip, None)
            session.permanent = True
            # Preserve the casing the user typed so the header chip reads
            # "Jay" / "JD" the way they signed in, not the env-var spelling.
            session["user"] = username
            return redirect(next_url)
        _login_record_fail(ip)
        error = "Invalid username or password."

    return render_template("login.html", error=error, next_url=next_url)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/favicon.ico")
def favicon_ico():
    """Direct /favicon.ico requests bypass the template's <link> tag.
    Send the static file straight from /static/favicon.ico so the tab
    icon shows up even when the browser doesn't read the head first."""
    return app.send_static_file("favicon.ico")


@app.route("/")
def index():
    # Humans must be logged in to see the dashboard. The API token is for
    # scripts hitting /api/*, not for serving the HTML page.
    if not _user_logged_in():
        return redirect(url_for("login", next=request.full_path or "/"))
    return render_template("index.html", current_user=session.get("user", ""))


# ---------------------------------------------------------------------------
# API – Inventory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# API – Chefs Warehouse POs
# ---------------------------------------------------------------------------
# CW POs live in their own JSON file (data/chefs_warehouse_pos.json) so
# they never touch inventory.json. The Pending POs tab merges them in
# for display via /api/chefs-warehouse/pos; the Inventory tab never
# shows them.


# ---------------------------------------------------------------------------
# API – Freight (Lineage outbound shipping invoices)
# ---------------------------------------------------------------------------
# Lineage Freight Management LLC invoices arrive in the same mailbox as
# distributor POs (sender: noreply@tms.blujaysolutions.net, subject:
# "Billable Invoice(s) from LINEAGE FREIGHT MANAGEMENT LLC"). The
# attachment is a .zip containing one or more PDF invoices, one per
# shipment H&H sent to a DC. We parse the PDFs in the cron-side scan
# script (scripts/cowork_graph_scan.py) and POST the dict-form records
# here, so the web service doesn't need outbound network access.
#
# Records live in data/freight_invoices.json and never touch inventory
# state. The Freight Costs tab reads /api/freight/invoices for display.


# Freight invoices/ingest/scan routes moved to blueprints/freight.py.


# ---------------------------------------------------------------------------
# API – Arrived (rolled-over) inventory POs
# ---------------------------------------------------------------------------
# When a USF/Cheney on_order entry's trigger date passes,
# inventory_tracker._rollover_on_order() drops it from item["on_order"]
# and _append_rollover_usage() writes a usage row tagged
# source="on_order_rollover". Those usage rows are the ONLY persistent
# record that an inventory-side PO arrived (the on_order entry is gone),
# so the Pending POs tab can't show arrived USF/Cheney POs from
# /api/inventory alone — that's why "Arrived" historically showed only
# Chefs Warehouse POs (which keep their own record). This endpoint
# reconstructs arrived inventory POs by grouping those usage rows by
# po_number so the frontend can merge them into the Arrived view.

# ---------------------------------------------------------------------------
# Freight -> PO ship-date linkage
# ---------------------------------------------------------------------------
# Lineage freight invoices carry the H&H PO number (plus order_number /
# shipper_ref) and the real ship_date. Arrived POs frequently have no
# ship_date — inventory POs lose it when they roll over, and some POs never
# had one entered — so we link by PO number to backfill the ship date for
# display in the Pending POs "Arrived" view.


# _freight_ship_date_index + ship-date-index + lead-times moved to blueprints/freight.py.


# ---------------------------------------------------------------------------
# API – Daily Production
# ---------------------------------------------------------------------------
# Production sheets are separate from inventory. Each record describes
# what was baked for a particular PO on a particular day, parsed from
# the Daily Production Sheet PDF that the production team emails out.


# ---------------------------------------------------------------------------
# API – $PLH report (production revenue per labor hour at the bakery)
# ---------------------------------------------------------------------------
# Per-case sell prices (revenue side):
#   US Foods           $27.00
#   Cheney Brothers    $26.50
#   anything else      $29.50   (Chefs Warehouse, unassigned, etc.)
# Default labor rate $17/hr used to back-fill `dollars` on a labor entry
# that only carries `hours`. PLH = revenue / labor_hours.


# ---------------------------------------------------------------------------
# API -- Bakery weekly sales (used while Toast is not connected for the
# production bakery). Source is the "Bakery Model - Sales v. Labor"
# spreadsheet JD updates weekly.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# API – Forecast bridge (weekly_usage -> usage ledger, so lots burn down)
# ---------------------------------------------------------------------------
# Background: every SKU carries a `weekly_usage` rate (cs/wk) but nothing in
# the system converted that rate into actual usage events, so production
# lots never showed cs_consumed > 0 at the DCs. The user's request: bridge
# the rate into the usage ledger so FIFO lot consumption works, with a
# weekly true-up that reconciles forecasts against vendor on-hand snapshots.
#
# Design:
#   - /api/forecast/decrement-daily   Idempotent per (sku, UTC date).
#                                     Posts +weekly_usage/7 as a positive
#                                     usage event tagged source="forecast-daily"
#                                     and decrements item quantity by the
#                                     same amount. Triggered by the daily
#                                     Render cron (bagel-inventory-forecast-daily).
#   - /api/forecast/true-up           Operator posts a vendor on-hand
#                                     snapshot. We reverse every uncovered
#                                     "forecast-daily" entry since the prior
#                                     truth, post the actual usage as
#                                     source="vendor-truth", and set
#                                     quantity = reported_qty.
#
# Both write directly through load_usage/save_usage so they can tag entries
# with a custom `source` (the existing record_usage helper hard-codes the
# absence of one). The FIFO lot consumption logic in
# inventory_tracker.compute_lot_fifo_state already honors `reversed: true`
# and skips `source == "reversal"`, so the moving parts are minimal.


# ---------------------------------------------------------------------------
# API – Usage
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# API – Report
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Warehouse catalogue (authoritative list used by UI + seeds)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# API – Distributors (unified view across Cheney Brothers and US Foods)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# API – Sync (pull current on-hand from distributors)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Microsoft Graph webhook/subscription routes — moved to blueprints/webhooks.py.
#
# These three routes turn the Daily Production pipeline into a push model:
#   - /webhooks/graph/notifications   Graph -> us. New-message pings; we
#                                     fetch + parse + ingest within seconds
#                                     of the email landing in the inbox.
#   - /api/graph/subscriptions        POST = create subs for every mailbox
#                                     in MS365_USER. GET = list local state.
#   - /api/graph/subscriptions/renew  POST = PATCH each sub to push its
#                                     expiration out ~68h. Mail subs cap at
#                                     71h; a daily Render cron calls this.
#
# The webhook deliberately lives OUTSIDE /api/ so the _gate_writes middleware
# doesn't reject it — Graph won't send our INVENTORY_API_TOKEN header.
# Instead, every notification carries a clientState (set via
# GRAPH_SUB_CLIENT_STATE) which the handler verifies.
# ---------------------------------------------------------------------------

# Graph webhook + subscription routes now live in blueprints/webhooks.py.


if __name__ == "__main__":
    app.run(debug=False, port=5000)


