"""Microsoft Graph webhook + subscription blueprint (extracted from app.py).

- POST /webhooks/graph/notifications  Graph -> us: a validation handshake or a
  real notification batch. Authenticates via clientState (GRAPH_SUB_CLIENT_STATE),
  which is why it lives outside /api/ and is on the open-endpoint allowlist as
  "webhooks.graph_webhook_notifications".
- /api/graph/subscriptions            GET = list local state, POST = create one
  per mailbox in MS365_USER.
- POST /api/graph/subscriptions/renew  PATCH each sub (~68h); a daily Render cron
  calls this.
"""
from flask import Blueprint, jsonify, make_response, request

from core.errors import _log_exc, _safe_err

webhooks_bp = Blueprint("webhooks", __name__)

# Character allowlist for the Graph webhook validation-token echo.
_VALIDATION_TOKEN_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.~+/= ")


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
        if not _re_vt.fullmatch(r"[A-Za-z0-9_\-.~+/= ]{1,4096}", validation_token):
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
    try:
        from integrations.graph_subscriptions import (
            handle_notification, record_notification_event,
        )
        handle_notification(payload)
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "webhook worker")
        try:
            record_notification_event(error="worker crashed (see server log)")
        except Exception:  # noqa: BLE001
            pass


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
