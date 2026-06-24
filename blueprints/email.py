"""Email blueprint — the MS365 mailbox scan, outbound send (Graph/SMTP), and
the ingest-events endpoint. Extracted from app.py (refactor — see
REFACTOR_PLAN.md). Shared helpers come from core/."""
import os
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from core.errors import _log_exc, _safe_err
from core.http import _TRUSTED_OUTBOUND_HOSTS

email_bp = Blueprint("email", __name__)


@email_bp.route("/api/email/scan", methods=["POST"])
def api_email_scan():
    # The whole route runs inside one try block so that ANY failure (import,
    # JSON parse, scan_email itself) becomes a structured 200 with status:
    # "error" + a traceback excerpt -- never a generic Flask 500. That's a
    # lot easier to debug from the client side when there are no logs handy.
    #
    # Bound the work so we don't blow past gunicorn's worker timeout. The
    # original default (300 messages * N mailboxes, each pulling full MIME
    # bytes via Graph) regularly killed the worker on Render's starter
    # plan, which surfaced as a generic 500 at the edge regardless of the
    # try/except below. Callers can override `max_messages` in the body or
    # set MS365_FILTER for a wider sweep done out of band.
    import traceback as _tb
    dry_run = False
    try:
        from integrations.email_scanner import EmailInboxClient
        from sync_inventory import _apply_events
        body = request.json or {}
        dry_run = bool(body.get("dry_run", False))
        # Default keeps within gunicorn's 180s budget (60 messages * 2
        # mailboxes is comfortable). The hard cap of 2000 lets ad-hoc deep
        # sweeps go further when paired with a narrowing MS365_FILTER; the
        # 180s worker timeout still binds, so callers requesting more than a
        # few hundred without a filter should expect a 504.
        try:
            max_messages = int(body.get("max_messages") or 60)
        except (TypeError, ValueError):
            max_messages = 60
        max_messages = max(1, min(max_messages, 2000))

        # Optional wide-lookback override. Without this, the scan uses
        # whatever MS365_FILTER is set to on the service (typically empty,
        # which returns the most recent max_messages). Pass `lookback_days`
        # to one-shot a deeper sweep -- useful for backfilling on_order
        # entries whose source emails predate the normal scan window.
        filter_override = None
        try:
            lookback_days = int(body.get("lookback_days") or 0)
        except (TypeError, ValueError):
            lookback_days = 0
        if lookback_days > 0:
            from datetime import datetime, timezone, timedelta
            since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
            # Graph wants ISO 8601 with a Z suffix, no microseconds.
            iso = since.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            # Bound the sweep by date only -- deliberately NOT by
            # hasAttachments. A hasAttachments filter drops body-pasted
            # reports server-side (e.g. the US Foods Zebulon weekly inventory
            # report, whose only "attachment" is an inline signature image),
            # so those never reached the parser. _scan_ms365_mailbox
            # pre-qualifies every listed message by sender/recipient (known
            # distributor domain or mapped report/worksheet rep) before
            # downloading any MIME, so dropping hasAttachments widens coverage
            # without ballooning the gunicorn budget. A plain receivedDateTime
            # filter also keeps $orderby working (no InefficientFilter).
            filter_override = f"receivedDateTime ge {iso}"

        client = EmailInboxClient()
        try:
            scan = client.scan(max_messages=max_messages,
                               filter_override=filter_override)
        except Exception as exc:  # noqa: BLE001 — surface NotConfigured + transport
            report = {
                "distributor": "Email Inbox",
                "source": client.source(),
                "status": ("not_configured" if type(exc).__name__ == "NotConfiguredError"
                           else "error"),
                "fetched": 0, "updated": 0, "unchanged": 0,
                "unmatched": [], "changes": [],
                "error": _safe_err(exc),
                "traceback": "",
                "messages_seen": 0, "messages_parsed": 0,
                "by_event_type": {"on_hand": 0, "restock": 0, "usage": 0},
            }
            return jsonify({"dry_run": dry_run, "reports": [report]})

        report = _apply_events(
            events=list(scan.events),
            messages_seen=scan.messages_seen,
            messages_parsed=scan.messages_parsed,
            errors=list(scan.errors or []),
            dry_run=dry_run,
            source=client.source(),
        )
        # Persist a scan-health heartbeat + capture recognized-but-unparsed
        # distributor mail (real runs only) so a missed warehouse or parser
        # gap is observable on /api/scan/health (roadmap #2/#5/#6).
        if not dry_run:
            try:
                from inventory_tracker import (
                    save_scan_health, record_unparsed_reports,
                )
                _unparsed = list(getattr(scan, "unparsed", []) or [])
                if _unparsed:
                    record_unparsed_reports(_unparsed)
                save_scan_health({
                    "ts": datetime.now().isoformat(),
                    "source": report.get("source"),
                    "status": report.get("status"),
                    "messages_seen": report.get("messages_seen"),
                    "messages_parsed": report.get("messages_parsed"),
                    "updated": report.get("updated"),
                    "unchanged": report.get("unchanged"),
                    "by_event_type": report.get("by_event_type"),
                    "had_errors": bool(report.get("error")),
                    "unparsed_count": len(_unparsed),
                })
            except Exception as exc:  # noqa: BLE001 — health is best-effort
                _log_exc(exc, "scan health persist")
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "email scan")
        report = {
            "distributor": "Email Inbox",
            "source": "unknown",
            "status": "error",
            "fetched": 0,
            "updated": 0,
            "unchanged": 0,
            "unmatched": [],
            "changes": [],
            "error": "internal error",
            "traceback": "",
            "messages_seen": 0,
            "messages_parsed": 0,
        }
    # Strip any exception-derived text the scanner surfaced into report["error"]
    # before returning; full detail is in the server log. Keeps exception text
    # out of the HTTP response (CodeQL py/stack-trace-exposure, inline barrier).
    _scan_err = report.get("error")
    if _scan_err:
        import sys as _sys
        print(f"[email scan errors] {_scan_err}", file=_sys.stderr)
        report["error"] = (
            "scan completed with errors (see server log)"
            if report.get("status") == "ok" else "internal error"
        )
    return jsonify({"dry_run": dry_run, "reports": [report]})


@email_bp.route("/api/scan/health")
def api_scan_health():
    """Scan heartbeat + per-warehouse count freshness (alerting / dashboard).

    Returns the last real scan's summary, every warehouse's days-since-count
    with a stale flag (older than STALE_COUNT_DAYS or never counted), and the
    queue of recognized-but-unparsed distributor messages. Read-only; gated by
    the global auth hook (browser session or X-Inventory-Token).
    """
    try:
        from inventory_tracker import (
            load_scan_health, warehouse_freshness, load_unparsed_reports,
            STALE_COUNT_DAYS,
        )
        health = load_scan_health()
        fresh = warehouse_freshness()
        stale = [r for r in fresh if r.get("stale")]
        scan_age_hours = None
        last_ts = health.get("ts") if health else None
        if last_ts:
            try:
                dt = datetime.fromisoformat(last_ts)
                scan_age_hours = round(
                    (datetime.now() - dt).total_seconds() / 3600.0, 1)
            except ValueError:
                pass
        return jsonify({
            "ok": True,
            "last_scan": health or None,
            "last_scan_age_hours": scan_age_hours,
            "stale_count_days": STALE_COUNT_DAYS,
            "stale_warehouse_count": len(stale),
            "warehouses": fresh,
            "stale_warehouses": stale,
            "unparsed_reports": load_unparsed_reports(),
        })
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "scan health")
        return jsonify({"ok": False, "error": "internal error"}), 500


@email_bp.route("/api/email/send", methods=["POST"])
def api_email_send():
    """Send an email from the configured M365 mailbox via Microsoft Graph.

    Uses the same app-only client-credentials token the mailbox scan uses
    (EmailInboxClient._ms365_token), so the app registration must have the
    **Mail.Send** application permission with admin consent. Protected by the
    global _gate_requests() before_request hook: any POST to /api/* requires a
    browser session or the X-Inventory-Token header.

    Body:
        {
          "to": "a@x.com" | ["a@x.com", ...],     # required
          "cc": "b@x.com" | [...],                # optional
          "subject": "...",                        # subject for new mail
          "body": "plain text",                    # default Text content
          "html": "<p>..</p>",                     # optional; overrides body
          "from": "jd@hhbagels.com",               # optional sender UPN
          "reply_to_message_id": "<graph id>",     # optional; reply in-thread
          "save_to_sent_items": true,              # optional, default true
          "dry_run": false                         # optional; build, don't send
        }

    Always returns a structured 200 (never a bare 500):
      {"ok": true, "sent": true, ...} or {"ok": false, "error": "..."}.
    """
    import json as _json
    import traceback as _tb
    import urllib.request
    import urllib.error
    import urllib.parse
    try:
        from integrations.email_scanner import EmailInboxClient, GRAPH_BASE

        body = request.get_json(silent=True) or {}

        def _as_list(v):
            if not v:
                return []
            if isinstance(v, str):
                return [v.strip()] if v.strip() else []
            return [str(x).strip() for x in v if str(x).strip()]

        to = _as_list(body.get("to"))
        cc = _as_list(body.get("cc"))
        if not to:
            return jsonify({"ok": False, "error": "missing required field 'to'"}), 200

        subject = (body.get("subject") or "").strip()
        html = body.get("html")
        if html:
            content_type, content = "HTML", html
        else:
            content_type, content = "Text", (body.get("body") or "")

        dry_run = bool(body.get("dry_run", False))
        save_to_sent = bool(body.get("save_to_sent_items", True))
        reply_to = (body.get("reply_to_message_id") or "").strip()

        # Sender: explicit 'from' wins, else MS365_SEND_AS, else first MS365_USER.
        sender = (body.get("from") or os.environ.get("MS365_SEND_AS") or "").strip()
        if not sender:
            users = [u.strip() for u in os.environ.get("MS365_USER", "").split(",") if u.strip()]
            sender = users[0] if users else ""
        if not sender:
            return jsonify({"ok": False, "error": "no sender configured (set MS365_SEND_AS or MS365_USER, or pass 'from')"}), 200

        if dry_run:
            return jsonify({"ok": True, "sent": False, "dry_run": True,
                            "from": sender, "to": to, "cc": cc,
                            "subject": subject, "threaded": bool(reply_to)})

        client = EmailInboxClient()
        token = client._ms365_token()
        uq = urllib.parse.quote

        def _recips(addrs):
            return [{"emailAddress": {"address": a}} for a in addrs]

        def _graph(method, url, data=None):
            payload = None if data is None else _json.dumps(data).encode("utf-8")
            headers = {"Authorization": f"Bearer {token}"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            if (urllib.parse.urlparse(url).hostname or "").lower() not in _TRUSTED_OUTBOUND_HOSTS:
                raise ValueError("refusing outbound request to untrusted host")
            req = urllib.request.Request(url, data=payload, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (raw.decode("utf-8", "replace") if raw else "")

        try:
            if reply_to:
                # Reply in-thread: createReply -> patch body/recipients -> send.
                base = f"{GRAPH_BASE}/users/{uq(sender)}/messages/{uq(reply_to)}"
                _, draft_raw = _graph("POST", base + "/createReply")
                draft = _json.loads(draft_raw or "{}")
                draft_id = draft.get("id")
                if not draft_id:
                    return jsonify({"ok": False, "error": f"createReply returned no draft id: {draft_raw[:300]}"}), 200
                patch = {"body": {"contentType": content_type, "content": content},
                         "toRecipients": _recips(to)}
                if cc:
                    patch["ccRecipients"] = _recips(cc)
                if subject:
                    patch["subject"] = subject
                _graph("PATCH", f"{GRAPH_BASE}/users/{uq(sender)}/messages/{uq(draft_id)}", patch)
                _graph("POST", f"{GRAPH_BASE}/users/{uq(sender)}/messages/{uq(draft_id)}/send")
            else:
                message = {"subject": subject,
                           "body": {"contentType": content_type, "content": content},
                           "toRecipients": _recips(to)}
                if cc:
                    message["ccRecipients"] = _recips(cc)
                _graph("POST", f"{GRAPH_BASE}/users/{uq(sender)}/sendMail",
                       {"message": message, "saveToSentItems": save_to_sent})
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:  # noqa: BLE001
                detail = ""
            return jsonify({"ok": False,
                            "error": _safe_err(exc, "graph send"),
                            "detail": ""}), 200

        return jsonify({"ok": True, "sent": True, "dry_run": False,
                        "from": sender, "to": to, "cc": cc,
                        "subject": subject, "threaded": bool(reply_to)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc),
                        "traceback": ""}), 200


@email_bp.route("/api/email/ingest-events", methods=["POST"])
def api_email_ingest_events():
    """Accept externally-parsed EmailEvents and apply them through the same
    PO revision-replace pipeline as /api/email/scan.

    Used by the Cowork scheduled routine that reads M365 mailboxes via the
    Outlook MCP, parses attachments client-side, and POSTs events here -- so
    the web service never needs outbound Graph credentials.

    Request body:
        {
          "dry_run": false,
          "source": "cowork-routine",        # free-form tag for the report
          "messages_seen": 12,
          "messages_parsed": 3,
          "errors": ["..."],
          "events": [
            {
              "event_type": "restock"|"on_hand"|"usage",
              "item": {
                "quantity": 24.0,
                "distributor": "US Foods",
                "name": "Plain Bagel 4oz [USF - Manassas]",   # optional
                "variety": "Plain",                            # optional
                "warehouse": "Manassas, VA",                   # optional
                "unit": "each",                                # optional
                "price": 0.0,                                  # optional
                "case_cost": 27.0,                             # optional
                "case_size": 168,                              # optional
                "weekly_usage": 0                              # optional
              },
              "source_message_id": "...",
              "source_subject": "...",
              "po_number": "2125123456",   # required for PO revision semantics
              "po_revision": "1",          # numeric string; "" allowed
              "po_order_date": "2026-05-13" # ISO YYYY-MM-DD from the PO PDF;
                                            #   anchors ordered_at + ETA in
                                            #   the on_order entry. Optional.
            }, ...
          ]
        }
    """
    import traceback as _tb
    try:
        from integrations import EmailEvent, SyncItem
        from sync_inventory import _apply_events
    except Exception as exc:  # noqa: BLE001
        return jsonify({
            "dry_run": False,
            "reports": [{
                "distributor": "Email Inbox",
                "source": "external",
                "status": "error",
                "fetched": 0,
                "updated": 0,
                "unchanged": 0,
                "unmatched": [],
                "changes": [],
                "error": _safe_err(exc, "import"),
                "traceback": "",
                "messages_seen": 0,
                "messages_parsed": 0,
            }],
        })

    payload = request.json or {}
    dry_run = bool(payload.get("dry_run", False))
    source = str(payload.get("source") or "external").strip() or "external"
    messages_seen = int(payload.get("messages_seen") or 0)
    messages_parsed = int(payload.get("messages_parsed") or 0)
    errors = list(payload.get("errors") or [])
    raw_events = payload.get("events") or []

    if not isinstance(raw_events, list):
        return jsonify({"ok": False, "error": "events must be a list"}), 400

    # Filter out events for POs that were canceled by the operator. The
    # source emails may still live in the inbox so the scanner keeps
    # surfacing them — we silently drop them here so the cancel sticks.
    from inventory_tracker import load_canceled_pos
    canceled = load_canceled_pos()
    canceled_skipped = 0
    if canceled:
        kept_events = []
        for ev in raw_events:
            po = str((ev or {}).get("po_number") or "").strip()
            if po and po in canceled:
                canceled_skipped += 1
                continue
            kept_events.append(ev)
        raw_events = kept_events

    built: list[EmailEvent] = []
    build_errors: list[str] = []
    for idx, e in enumerate(raw_events):
        if not isinstance(e, dict):
            build_errors.append(f"events[{idx}]: not an object")
            continue
        try:
            etype = e.get("event_type")
            if etype not in ("on_hand", "restock", "usage", "usage_rate"):
                build_errors.append(f"events[{idx}]: bad event_type {etype!r}")
                continue
            raw_item = e.get("item") or {}
            item = SyncItem(
                quantity=float(raw_item.get("quantity") or 0),
                distributor=str(raw_item.get("distributor") or ""),
                name=raw_item.get("name"),
                variety=raw_item.get("variety"),
                warehouse=raw_item.get("warehouse"),
                unit=raw_item.get("unit"),
                price=(float(raw_item["price"])
                       if raw_item.get("price") is not None else None),
                distributor_sku=raw_item.get("distributor_sku"),
                case_cost=(float(raw_item["case_cost"])
                           if raw_item.get("case_cost") is not None else None),
                case_size=(int(raw_item["case_size"])
                           if raw_item.get("case_size") is not None else None),
                weekly_usage=(float(raw_item["weekly_usage"])
                              if raw_item.get("weekly_usage") is not None else None),
            )
            built.append(EmailEvent(
                event_type=etype,
                item=item,
                source_message_id=str(e.get("source_message_id") or ""),
                source_subject=str(e.get("source_subject") or ""),
                po_number=str(e.get("po_number") or ""),
                po_revision=str(e.get("po_revision") or ""),
                po_order_date=str(e.get("po_order_date") or ""),
                count_date=str(e.get("count_date") or ""),
            ))
        except (TypeError, ValueError, KeyError) as exc:
            build_errors.append(f"events[{idx}]: error")

    try:
        all_errors = errors + build_errors
        if canceled_skipped:
            all_errors.append(f"skipped {canceled_skipped} event(s) for canceled POs")
        report = _apply_events(
            events=built,
            messages_seen=messages_seen,
            messages_parsed=messages_parsed,
            errors=all_errors,
            dry_run=dry_run,
            source=source,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({
            "dry_run": dry_run,
            "reports": [{
                "distributor": "Email Inbox",
                "source": source,
                "status": "error",
                "fetched": len(built),
                "updated": 0, "unchanged": 0,
                "unmatched": [], "changes": [],
                "error": _safe_err(exc),
                "messages_seen": messages_seen,
                "messages_parsed": messages_parsed,
            }],
        }), 500

    return jsonify({"dry_run": dry_run, "reports": [report]})
