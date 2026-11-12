"""Authentication helpers shared across the app and blueprints.

Session-based login for the browser, INVENTORY_API_TOKEN header for cron jobs
and scripts. Kept free of any app/blueprint import so anything can depend on it.
"""
import hashlib
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


def _allowed_users() -> set:
    """Lower-cased login allowlist: INVENTORY_USERNAMES (comma-separated),
    falling back to the legacy single INVENTORY_USERNAME."""
    raw = (os.environ.get("INVENTORY_USERNAMES")
           or os.environ.get("INVENTORY_USERNAME", ""))
    return {u.strip().lower() for u in raw.split(",") if u.strip()}


def password_version() -> str:
    """Short fingerprint of the current shared password. Stored in the session
    at login (session["pwv"]) so changing INVENTORY_PASSWORD signs every
    browser out instead of leaving 30-day cookies alive."""
    pw = os.environ.get("INVENTORY_PASSWORD", "")
    return hashlib.sha256(("pwv:" + pw).encode("utf-8")).hexdigest()[:12]


def _require_pwv() -> bool:
    return (os.environ.get("SESSION_REQUIRE_PWV", "off").strip().lower()
            in {"on", "1", "true", "yes"})


def _user_logged_in() -> bool:
    user = session.get("user")
    if not user:
        return False
    pwv = session.get("pwv")
    if pwv is None:
        # A session from before pwv existed. Kept working until the operator
        # sets SESSION_REQUIRE_PWV=on, so a deploy signs nobody out.
        return not _require_pwv()
    # A session issued since: the user must still be on the allowlist and the
    # password must not have changed.
    if str(user).strip().lower() not in _allowed_users():
        return False
    return secrets.compare_digest(str(pwv).encode("utf-8"),
                                  password_version().encode("utf-8"))


def derive_secret_key() -> tuple:
    """(key, source) for Flask's session signing.

    FLASK_SECRET_KEY / SECRET_KEY when set. Otherwise a key derived from
    INVENTORY_API_TOKEN (or, failing that, INVENTORY_PASSWORD): the same in
    both gunicorn workers and across deploys. The old fallback was a random
    key per process, so with two workers a login worked on one and bounced on
    the other, and every deploy signed everyone out. Never raises -- a
    missing variable must not stop the app from booting."""
    for name in ("FLASK_SECRET_KEY", "SECRET_KEY"):
        val = os.environ.get(name)
        if val:
            return val, name
    for name in ("INVENTORY_API_TOKEN", "INVENTORY_PASSWORD"):
        val = (os.environ.get(name) or "").strip()
        if val:
            key = hashlib.sha256(
                ("inventory-tracker-session-key:" + val).encode("utf-8")
            ).hexdigest()
            return key, "derived:" + name
    return secrets.token_hex(32), "random"


def _presented_token() -> str:
    return (request.headers.get("X-Inventory-Token") or "").strip()


def _eq(a: str, b: str) -> bool:
    # bytes: compare_digest raises TypeError on non-ASCII str (a 500 from the
    # auth gate instead of a 401).
    return bool(a) and bool(b) and secrets.compare_digest(
        a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# Named, scoped tokens (additive). INVENTORY_API_TOKEN stays the master key.
#   INVENTORY_API_TOKENS="name:token:scope,name2:token2:scope+scope"
# so each scheduled task can carry its own key, limited to what it does, and
# the activity log says which one acted. Same idea as DELIVERY_TOKEN_ENDPOINTS
# below, expressed as (method, path prefix) rules.
# ---------------------------------------------------------------------------
TOKEN_SCOPES = ("full", "read", "ingest", "send", "backup")

SCOPE_RULES = {
    "read": [("GET", "/api/")],
    "ingest": [
        ("POST", "/api/email/scan"),
        ("POST", "/api/email/ingest-events"),
        ("POST", "/api/ingest/"),
        ("POST", "/api/freight/ingest"),
        ("POST", "/api/freight/scan"),
        ("POST", "/api/production/ingest"),
        ("POST", "/api/production/scan"),
        ("POST", "/api/sales/ingest"),
        ("POST", "/api/chefs-warehouse/ingest-pos"),
        ("POST", "/api/admin/labor/ingest"),
        ("POST", "/api/admin/bakery-sales/ingest"),
        ("POST", "/api/toast/nightly-pull"),
        ("GET", "/api/scan/health"),
        ("GET", "/api/feeds/health"),
    ],
    "send": [
        ("POST", "/api/email/send"),
        ("GET", "/api/report-status"),
        ("GET", "/api/scan/health"),
        ("GET", "/api/feeds/health"),
    ],
    "backup": [
        ("POST", "/api/admin/backup/run"),
        ("POST", "/api/admin/backup/offsite-ack"),
        ("GET", "/api/admin/backups"),
        ("GET", "/api/feeds/health"),
    ],
}
# A read-only key reads the app's views, not whole-disk backup archives.
_READ_SCOPE_DENY = ("/api/admin/backups",)


def _named_tokens() -> list:
    """[(name, token, {scopes})] from INVENTORY_API_TOKENS. Malformed entries
    and unknown scopes are skipped (never an error at request time)."""
    out = []
    for part in (os.environ.get("INVENTORY_API_TOKENS") or "").split(","):
        bits = [b.strip() for b in part.strip().split(":")]
        if len(bits) != 3 or not bits[0] or not bits[1]:
            continue
        scopes = {x for x in bits[2].lower().replace("|", "+").split("+")
                  if x in TOKEN_SCOPES}
        if scopes:
            out.append((bits[0], bits[1], scopes))
    return out


def token_identity():
    """(name, scopes) for the X-Inventory-Token on this request, else None.
    The master INVENTORY_API_TOKEN is ("", {"full"})."""
    got = _presented_token()
    if not got:
        return None
    if _eq(got, os.environ.get("INVENTORY_API_TOKEN", "").strip()):
        return "", {"full"}
    found = None
    for name, tok, scopes in _named_tokens():
        if _eq(got, tok) and found is None:
            found = (name, scopes)
    return found


def _path_matches(path: str, prefix: str) -> bool:
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix or path.startswith(prefix + "/")


def scope_allows(scopes, method: str, path: str) -> bool:
    """May a token holding `scopes` make this request?"""
    if "full" in scopes:
        return True
    method = "GET" if method == "HEAD" else method
    for scope in scopes:
        for m, prefix in SCOPE_RULES.get(scope, ()):
            if m != method or not _path_matches(path, prefix):
                continue
            if scope == "read" and any(_path_matches(path, d)
                                       for d in _READ_SCOPE_DENY):
                continue
            return True
    return False


def _has_valid_api_token() -> bool:
    """A FULL-access token: the master INVENTORY_API_TOKEN, or a named token
    whose scope is `full`."""
    ident = token_identity()
    return bool(ident) and "full" in ident[1]


def _is_authenticated() -> bool:
    return _user_logged_in() or _has_valid_api_token()


def _admins() -> set:
    return {u.strip().lower()
            for u in (os.environ.get("INVENTORY_ADMINS") or "").split(",")
            if u.strip()}


def is_admin() -> bool:
    """May this request run the destructive maintenance actions (seed reset,
    restore, backup restore)? A full-access token always may. A logged-in
    user may when INVENTORY_ADMINS is unset (everyone, as before) or names
    them."""
    if _has_valid_api_token():
        return True
    if not _user_logged_in():
        return False
    admins = _admins()
    return not admins or str(session.get("user") or "").strip().lower() in admins


def admin_required(fn):
    """View decorator: 403 unless is_admin()."""
    import functools

    from flask import jsonify

    @functools.wraps(fn)
    def _inner(*args, **kwargs):
        if not is_admin():
            return jsonify({"ok": False, "error": "admin only"}), 403
        return fn(*args, **kwargs)
    return _inner


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
    return _eq(_presented_token(), os.environ.get("DELIVERY_API_TOKEN", "").strip())
