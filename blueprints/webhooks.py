"""Microsoft Graph webhook + subscription blueprint (extracted from app.py).

- POST /webhooks/graph/notifications  Graph -> us: a validation handshake or a
  real notification batch. Authenticates via clientState (GRAPH_SUB_CLIENT_STATE),
  which is why it lives outside /api/ and is on the open-endpoint allowlist as
  "webhooks.graph_webhook_notifications". Daily Production sheets are ingested
  from it, and distributor / rep mail triggers a debounced mailbox scan
  (SCAN_ON_NOTIFY, default on).
- /api/graph/subscriptions            GET = list local state, POST = create one
  per mailbox in MS365_USER.
- POST /api/graph/subscriptions/renew  PATCH each sub (~68h); a daily Render cron
  calls this.
"""
from flask import Blueprint, jsonify, make_response, request

from core.errors import _log_exc, _safe_err

webhooks_bp = Blueprint("webhooks", __name__)

# Character allowlist for the Graph webhook validation-token echo. Graph's
# real token is "Validation: Testing client application reachability for
# subscription Request-Id: <guid>" -- it contains COLONS. The 2026-06-10
# CodeQL hardening left ':' out, every handshake got a 400, and Graph refused
# to renew or recreate the subscriptions: no real-time Daily Production
# notification has arrived since that day. ':' is inert in text/plain.
_VALIDATION_TOKEN_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.~+/=: ")


@webhooks_bp.route("/webhooks/graph/notifications", methods=["POST"])
def graph_webhook_notifications():
    """Microsoft Graph -> us. Either a validation handshake or a real
    notification batch.

    Graph requires:
      - The validation handshake (a one-time GET-or-POST with
        ``?validationToken=…``) returns the token verbatim as
        ``text/plain``. Anything else fails the subscription create.
      - A regular notification POST returns 2xx within 10 seconds, or
        Graph backs off and eventually disables the subscription.
    """
    validation_token = request.args.get("validationToken")
    if validation_token:
        import re as _re_vt
        if not _re_vt.fullmatch(r"[A-Za-z0-9_\-.~+/=: ]{1,4096}", validation_token):
            return make_response("invalid validation token", 400)
        # Rebuild from a fixed character allowlist so no request-derived value is
        # ever reflected verbatim (CodeQL py/reflective-xss); the fullmatch above
        # already guarantees this equals the original token. html.escape is a
        # recognized sanitizer and a no-op on the validated charset.
        import html as _html_vt
        safe_token = _html_vt.escape(
            "".join(c for c in validation_token if c in _VALIDATION_TOKEN_CHARS))
        resp = make_response(safe_token, 200)
        resp.headers["Content-Type"] = "text/plain"
        return resp

    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:  # noqa: BLE001
        payload = {}

    # Answer 202 as soon as the batch is validated and do the Graph fetches +
    # PDF parse in the background. Doing them inside the response (token,
    # message, attachment list, attachment, pypdf -- per message, for EVERY
    # inbound mail) is slow enough for Graph to throttle and drop the
    # endpoint; no notification was ingested after 2026-06-10.
    try:
        from integrations.graph_subscriptions import valid_notification_batch
        if not valid_notification_batch(payload):
            return jsonify({"ok": False,
                            "error": "no notification carried a valid clientState"}), 202
        import threading
        threading.Thread(target=_process_notifications, args=(payload,),
                         name="graph-notify", daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        # Don't 500 — that makes Graph back off and eventually disable the sub.
        # Log server-side and return a generic 202 so Graph stays happy.
        _log_exc(exc, "webhook")
        return jsonify({"ok": False, "error": "internal error (webhook)"}), 202

    return jsonify({"ok": True, "accepted": len(payload.get("value") or [])}), 202


def _process_notifications(payload: dict) -> None:
    """Background worker for a validated notification batch."""
    result = None
    try:
        from integrations.graph_subscriptions import (
            handle_notification, record_notification_event,
        )
        result = handle_notification(payload)
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "webhook worker")
        try:
            record_notification_event(error="worker crashed (see server log)")
        except Exception:  # noqa: BLE001
            pass
    # Distributor / rep mail in the batch -> scan the mailboxes now (debounced).
    # Runs here, in the background thread, never in the webhook response.
    try:
        senders = (result or {}).get("scan_senders") or []
        if senders:
            request_scan_on_notify(senders)
    except Exception as exc:  # noqa: BLE001 -- a trigger must never hurt ingest
        _log_exc(exc, "webhook scan trigger")


# --------------------------------------------------------------------------- #
# Scan when distributor mail arrives
# --------------------------------------------------------------------------- #
# The 6-hour cron means a PO or a weekly report can sit unread for hours. The
# app already receives a Graph notification for every new Inbox message, so
# when one is from a distributor or a covering rep we run the SAME scan the
# cron runs (blueprints.email.run_mailbox_scan: same code, same non-blocking
# ingest lock), debounced:
#
#   * at most one triggered scan per SCAN_DEBOUNCE_S (10 min), across both
#     gunicorn workers -- the claim is a timestamp in data/scan_trigger.json
#     taken under data_lock;
#   * the scan starts SCAN_SETTLE_S after the first notification, so a burst
#     (Ross's three workbooks, one mail delivered to JD@ and info@) is read by
#     one scan;
#   * a notification that arrives after that scan STARTED, inside the window,
#     is not dropped: it marks the state pending and one trailing scan runs
#     when the window closes.
#
# SCAN_ON_NOTIFY=off disables all of it (the cron is unaffected either way).
SCAN_DEBOUNCE_S = 600
SCAN_SETTLE_S = 30
SCAN_ON_NOTIFY_LOOKBACK_DAYS = 2
SCAN_ON_NOTIFY_MAX_MESSAGES = 200
_SCAN_TRIGGER_FILE = "scan_trigger.json"
_TRAILING = {"timer": None}


def _scan_on_notify_enabled() -> bool:
    import os
    return os.environ.get("SCAN_ON_NOTIFY", "on").strip().lower() not in (
        "off", "0", "false", "no")


def _now_ts() -> float:
    import time
    return time.time()


def _trigger_state_update(fn):
    """Load -> fn(state) -> save data/scan_trigger.json under data_lock.
    Returns whatever fn returns."""
    import inventory_tracker as _it
    with _it.data_lock():
        st = _it._read_json(_it.DATA_DIR / _SCAN_TRIGGER_FILE, {})
        if not isinstance(st, dict):
            st = {}
        out = fn(st)
        _it._write_json(_it.DATA_DIR / _SCAN_TRIGGER_FILE, st)
    return out


def request_scan_on_notify(senders=()) -> str:
    """A notification named distributor / rep mail. Returns what happened:
    "disabled", "scanned" (this call ran the scan), "covered" (a claimed scan
    has not started yet and will read this mail), or "debounced" (inside the
    window after a scan started; a trailing scan is pending)."""
    if not _scan_on_notify_enabled():
        return "disabled"
    now = _now_ts()

    def _claim(st):
        last = float(st.get("claimed_at") or 0)
        if now - last >= SCAN_DEBOUNCE_S:
            st.update(claimed_at=now, started_at=0, pending_since=0,
                      last_senders=list(senders)[:10])
            return "claim"
        if not float(st.get("started_at") or 0):
            if now - last > SCAN_SETTLE_S + 120:
                # Claimed long ago and never started (the worker was recycled
                # mid-settle): the claim is dead, take it over.
                st.update(claimed_at=now, started_at=0, pending_since=0,
                          last_senders=list(senders)[:10])
                return "claim"
            return "covered"            # claimed, still settling: it will see us
        if not float(st.get("pending_since") or 0):
            st["pending_since"] = now
        return "debounced"
    what = _trigger_state_update(_claim)
    if what == "claim":
        _sleep_settle()
        _run_triggered_scan()
        return "scanned"
    if what == "debounced":
        _schedule_trailing()
    return what


def _sleep_settle() -> None:
    import time
    if SCAN_SETTLE_S > 0:
        time.sleep(SCAN_SETTLE_S)


def _run_triggered_scan() -> dict:
    """Run the mailbox scan for a claimed trigger. Never raises."""
    def _started(st):
        st["started_at"] = _now_ts()
    try:
        _trigger_state_update(_started)
        from blueprints.email import run_mailbox_scan
        payload, status = run_mailbox_scan(
            {"lookback_days": SCAN_ON_NOTIFY_LOOKBACK_DAYS,
             "max_messages": SCAN_ON_NOTIFY_MAX_MESSAGES,
             "trigger": "webhook"},
            trigger="webhook")
        if status == 409:
            # Another ingest holds the lock (the cron, "Scan now"). Whatever
            # is running may have listed the mailbox before this mail landed:
            # leave a trailing scan pending.
            def _pend(st):
                if not float(st.get("pending_since") or 0):
                    st["pending_since"] = _now_ts()
            _trigger_state_update(_pend)
            _schedule_trailing()
        return payload
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "triggered scan")
        try:
            from core import feed_health
            feed_health.record("mailbox-scan", False,
                               "notification-triggered scan crashed (see server log)",
                               trigger="webhook")
        except Exception:  # noqa: BLE001
            pass
        return {}


def _schedule_trailing() -> None:
    """One in-process timer for when the debounce window closes."""
    import threading
    t = _TRAILING.get("timer")
    if t is not None and t.is_alive():
        return
    try:
        import inventory_tracker as _it
        st = _it._read_json(_it.DATA_DIR / _SCAN_TRIGGER_FILE, {}) or {}
        wait = max(1.0, float(st.get("claimed_at") or 0) + SCAN_DEBOUNCE_S
                   - _now_ts() + 1.0)
    except Exception:  # noqa: BLE001
        wait = float(SCAN_DEBOUNCE_S)
    t = threading.Timer(wait, _trailing_fire)
    t.daemon = True
    t.name = "scan-on-notify-trailing"
    _TRAILING["timer"] = t
    t.start()


def _trailing_fire() -> str:
    """The window closed: if mail arrived after the last triggered scan
    started, run one more -- unless the other worker's timer already did."""
    try:
        if not _scan_on_notify_enabled():
            return "disabled"
        now = _now_ts()

        def _claim(st):
            if not float(st.get("pending_since") or 0):
                return "nothing-pending"
            if now - float(st.get("claimed_at") or 0) < SCAN_DEBOUNCE_S:
                return "too-early"
            st.update(claimed_at=now, started_at=0, pending_since=0)
            return "claim"
        what = _trigger_state_update(_claim)
        if what == "claim":
            _run_triggered_scan()
            return "scanned"
        return what
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "trailing scan")
        return "error"


@webhooks_bp.route("/api/graph/subscriptions", methods=["GET", "POST"])
def api_graph_subscriptions():
    """GET = list locally-tracked subscriptions.
    POST = create a subscription per mailbox in MS365_USER.
    """
    try:
        from integrations.graph_subscriptions import (
            create_subscriptions, list_subscriptions,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc, "import")}), 500

    if request.method == "GET":
        return jsonify({"ok": True, "subscriptions": list_subscriptions()})

    try:
        result = create_subscriptions()
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "graph subscriptions create")
        return jsonify({"ok": False, "error": "internal error"}), 500
    return jsonify(result)


@webhooks_bp.route("/api/graph/subscriptions/renew", methods=["POST"])
def api_graph_subscriptions_renew():
    """Renew every tracked subscription. Called daily by a Render cron.

    If a subscription is missing on Graph's side (404), this recreates it so
    coverage doesn't silently lapse.
    """
    try:
        from integrations.graph_subscriptions import renew_subscriptions
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc, "import")}), 500
    try:
        result = renew_subscriptions()
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "graph subscriptions renew")
        return jsonify({"ok": False, "error": "internal error"}), 500
    return jsonify(result)
