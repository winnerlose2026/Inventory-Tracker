"""Email blueprint — the MS365 mailbox scan, outbound send (Graph/SMTP), and
the ingest-events endpoint. Extracted from app.py (refactor — see
REFACTOR_PLAN.md). Shared helpers come from core/."""
import contextlib
import os
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from core.errors import _log_exc, _safe_err
from core.http import _TRUSTED_OUTBOUND_HOSTS

email_bp = Blueprint("email", __name__)


@contextlib.contextmanager
def _ingest_lock():
    """Let only one inventory-writing ingest run at a time, across processes.

    gunicorn runs 2 workers x 4 threads, and every scan / ingest is a long
    read-modify-write of data/inventory.json. On 2026-09-17 two scans arrived
    in the same second, landed on different workers, and interleaved their
    writes; the file was left unparseable and the next save blanked it.

    Yields True when the lock was taken and False when another ingest already
    holds it -- callers answer 409 rather than queueing, because a scan that
    waits behind a 3-minute scan just dies on the worker timeout instead.
    """
    lock_path = os.path.join("data", ".ingest.lock")
    try:
        os.makedirs("data", exist_ok=True)
    except OSError:
        pass
    try:
        import fcntl
    except ImportError:  # Windows / desktop GUI runs -- single process anyway
        yield True
        return
    fh = open(lock_path, "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _token_header_ok() -> bool:
    """These mailbox-image routes demand the full API token header whenever one
    is configured (unchanged policy). Compared in constant time -- a plain !=
    leaks how many leading characters matched."""
    import hmac
    expected = os.environ.get("INVENTORY_API_TOKEN", "")
    if not expected:
        return True
    got = request.headers.get("X-Inventory-Token") or ""
    return hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))


def _busy_response(what):
    return jsonify({
        "ok": False,
        "status": "busy",
        "error": "another ingest is already running; retry shortly",
        "endpoint": what,
        "reports": [],
    }), 409


@email_bp.route("/api/email/scan", methods=["POST"])
def api_email_scan():
    with _ingest_lock() as got:
        if not got:
            return _busy_response("/api/email/scan")
        return _api_email_scan_locked()


def _api_email_scan_locked():
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
        # Chefs Warehouse POs ride alongside the events on the ScanResult but
        # land in their own store. sync_inventory.scan_email applied them;
        # this route -- the one the 6-hour cron and "Scan now" actually use --
        # did not, so a CW PO only ever arrived through the cowork fallback.
        cw_pos = list(getattr(scan, "cw_pos", None) or [])
        if cw_pos:
            try:
                from sync_inventory import _apply_cw_pos
                report["chefs_warehouse"] = _apply_cw_pos(
                    cw_pos, dry_run=dry_run, source=client.source())
            except Exception as exc:  # noqa: BLE001
                report.setdefault("errors", []).append(
                    f"chefs-warehouse apply failed: {_safe_err(exc)}")
        # Return the scanner's own errors instead of only "N parse error(s)".
        # Until 2026-10-22 a mailbox that stopped answering, or a PO dropped
        # whole for an unknown ship-to DC, left status "ok" and the cron log
        # said errors=0. Mailbox-level and whole-PO failures make the scan
        # "partial"; the cron exits non-zero on mailbox failures only.
        _scan_errors_summarize(report, list(scan.errors or []))
        # Persist a scan-health heartbeat + capture recognized-but-unparsed
        # distributor mail (real runs only) so a missed warehouse or parser
        # gap is observable on /api/scan/health (roadmap #2/#5/#6).
        if not dry_run:
            try:
                from inventory_tracker import (
                    save_scan_health, record_unparsed_reports,
                    prune_unparsed_reports, data_lock,
                )
                _unparsed = list(getattr(scan, "unparsed", []) or [])
                # Local file writes only (no network I/O) -> under data_lock.
                with data_lock():
                    if _unparsed:
                        record_unparsed_reports(_unparsed)
                    # Retire entries whose gap has closed (the warehouse
                    # counted after the message we couldn't read) or that have
                    # aged out, so the queue stays a live to-do list instead
                    # of an append-only log that buries real misses. Runs
                    # after this scan's events are applied, so a report
                    # ingested just now clears its own earlier failures.
                    prune_unparsed_reports()
                    save_scan_health({
                        "ts": datetime.now().isoformat(),
                        "source": report.get("source"),
                        "status": report.get("status"),
                        "messages_seen": report.get("messages_seen"),
                        "messages_parsed": report.get("messages_parsed"),
                        "updated": report.get("updated"),
                        "unchanged": report.get("unchanged"),
                        "by_event_type": report.get("by_event_type"),
                        "had_errors": bool(report.get("error")
                                           or report.get("errors")),
                        "unparsed_count": len(_unparsed),
                        "error_count": len(report.get("errors") or []),
                        "errors": (report.get("errors") or [])[:_HEALTH_MAX_LINES],
                        "mailbox_failures": report.get("mailbox_failures") or [],
                        "po_failures": report.get("po_failures") or [],
                        "warnings": ((report.get("warnings") or [])
                                     + (report.get("rejected") or [])
                                     )[:_HEALTH_MAX_LINES],
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
            if report.get("status") in ("ok", "partial") else "internal error"
        )
    return jsonify({"dry_run": dry_run, "reports": [report]})


# Cap on error/warning lines kept on the scan report and in scan_health.json.
_HEALTH_MAX_LINES = 50


def _scan_errors_summarize(report: dict, scan_errors: list) -> dict:
    """Attach the scanner's errors to an /api/email/scan report, classified.

    report["errors"]            every scan error (capped), then any the route
                                itself added (Chefs Warehouse apply)
    report["mailbox_failures"]  a whole mailbox could not be read
    report["po_failures"]       a whole PO document was dropped
    status "ok" -> "partial" when either of the last two is non-empty.

    The scanner builds these strings from message data only (exception text
    goes to the server log -- integrations.email_scanner._log_scan_exc), so
    they are safe to return; each is still flattened and length-capped.
    """
    from integrations.email_scanner import is_mailbox_failure, is_po_failure
    cleaned = [str(e).replace("\r", " ").replace("\n", " ")[:400]
               for e in (scan_errors or [])]
    route_errors = list(report.get("errors") or [])
    report["errors"] = cleaned[:_HEALTH_MAX_LINES * 2] + route_errors
    report["mailbox_failures"] = [e for e in cleaned if is_mailbox_failure(e)]
    report["po_failures"] = [e for e in cleaned if is_po_failure(e)]
    if report.get("status") == "ok" and (report["mailbox_failures"]
                                         or report["po_failures"]):
        report["status"] = "partial"
    return report


def _sku_count_gaps(inv: dict, stale_days: int) -> list:
    """SKUs the warehouse's count does NOT cover, per (distributor, warehouse).

    warehouse_freshness() is the NEWEST count across a warehouse's SKUs, so a
    SKU that no weekly report lists looks as fresh as the rest: on 2026-10-22
    Pumpernickel at Manassas and at Chicago had sat at 20 cs since 8/28, never
    counted, while both warehouses read "Counted Oct 19". A SKU is a gap when
    it has never been counted, or its last count is more than ``stale_days``
    older than its warehouse's newest count. A warehouse with no count at all
    is left to the stale-warehouse list. Same rule as the Inventory tab
    (static/app.js renderWarehouseCountGaps).
    """
    from inventory_tracker import _parse_iso_utc
    newest: dict = {}
    for item in (inv or {}).values():
        k = (item.get("distributor") or "", item.get("warehouse") or "")
        dt = _parse_iso_utc(item.get("last_count_at") or "")
        if dt is not None and (k not in newest or dt > newest[k]):
            newest[k] = dt
    gaps = []
    for key, item in sorted((inv or {}).items()):
        k = (item.get("distributor") or "", item.get("warehouse") or "")
        if k not in newest:
            continue
        dt = _parse_iso_utc(item.get("last_count_at") or "")
        if dt is None:
            reason = "never counted"
        elif (newest[k] - dt).total_seconds() > stale_days * 86400:
            reason = (f"last counted {dt.date().isoformat()}, "
                      f"{(newest[k] - dt).days} days before the warehouse's "
                      "latest count")
        else:
            continue
        gaps.append({
            "distributor": k[0], "warehouse": k[1], "sku": key,
            "name": item.get("name") or key,
            "quantity": item.get("quantity"),
            "last_count_at": item.get("last_count_at") or None,
            "warehouse_last_count_at": newest[k].isoformat(),
            "reason": reason,
        })
    return gaps


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
        # An empty inventory makes every downstream signal read "fine": no
        # SKUs means no warehouses, which means zero stale warehouses. That
        # false all-clear is what let the 2026-09-17 wipe sit unnoticed for
        # hours. Count the SKUs and say so plainly.
        from inventory_tracker import DataFileCorrupt, load_inventory
        inventory_error = None
        sku_gaps = []
        try:
            _inv = load_inventory()
            sku_count = len(_inv)
            sku_gaps = _sku_count_gaps(_inv, STALE_COUNT_DAYS)
        except DataFileCorrupt as exc:
            sku_count, inventory_error = -1, str(exc)
        except Exception as exc:  # noqa: BLE001
            _log_exc(exc, "scan health inventory count")
            sku_count, inventory_error = -1, "inventory unreadable"
        if sku_count == 0:
            inventory_error = ("inventory is EMPTY -- 0 SKUs on file; "
                               "warehouse freshness below is meaningless")
        # Real-time Daily Production ingest (Graph subscription): it sat dead
        # from 2026-06-10 with no signal anywhere, so say whether it's alive.
        try:
            from integrations.graph_subscriptions import subscription_health
            production_subscriptions = subscription_health()
        except Exception as exc:  # noqa: BLE001
            _log_exc(exc, "scan health production subscriptions")
            production_subscriptions = {"error": "unavailable"}
        # Pending POs <-> Inventory on-order agreement (blueprints/activity.py).
        # La Mirada 7135734C sat "In Production" on one tab and already in
        # on-hand on the other with no signal anywhere.
        try:
            from blueprints.activity import po_flow_check
            _flow = po_flow_check()
            po_flow = {"ok": _flow["ok"], "issue_count": _flow["issue_count"],
                       "issues": _flow["issues"], "notes": _flow["notes"]}
        except Exception as exc:  # noqa: BLE001
            _log_exc(exc, "scan health po flow")
            po_flow = {"ok": None, "error": "unavailable"}
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
            "ok": inventory_error is None,
            "inventory_sku_count": sku_count,
            "inventory_empty": sku_count == 0,
            "inventory_error": inventory_error,
            "last_scan": health or None,
            "last_scan_age_hours": scan_age_hours,
            "stale_count_days": STALE_COUNT_DAYS,
            "stale_warehouse_count": len(stale),
            "warehouses": fresh,
            "stale_warehouses": stale,
            # SKUs the warehouse's own count doesn't cover (see
            # _sku_count_gaps): a warehouse can be fresh while one of its SKUs
            # has never been counted.
            "sku_count_gaps": sku_gaps,
            "sku_count_gap_count": len(sku_gaps),
            "unparsed_reports": load_unparsed_reports(),
            "production_subscriptions": production_subscriptions,
            "po_flow": po_flow,
        })
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "scan health")
        return jsonify({"ok": False, "error": "internal error"}), 500


# /api/email/send recipient policy. Defaults cover every legitimate caller:
# the Mon/Tue chasers (US Foods + Cheney reps), Chefs Warehouse, Lineage, and
# the auto-send self-test to jd@. Override with EMAIL_SEND_ALLOWED_DOMAINS.
_SEND_DEFAULT_DOMAINS = ("usfoods.com", "cheneybrothers.com", "chefswarehouse.com",
                         "onelineage.com", "hhbagels.com")
# Extra senders a caller may name in 'from' besides MS365_SEND_AS. Default is
# the chasers' documented sender; override with EMAIL_SEND_ALLOWED_FROM.
_SEND_DEFAULT_FROM = ("jd@hhbagels.com",)
_PLAIN_ADDR_RE = None


def _env_list(name: str, default) -> list:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return [d.lower() for d in default]
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _send_allowed_domains() -> list:
    return _env_list("EMAIL_SEND_ALLOWED_DOMAINS", _SEND_DEFAULT_DOMAINS)


def _send_allowed_from() -> set:
    allowed = set(_env_list("EMAIL_SEND_ALLOWED_FROM", _SEND_DEFAULT_FROM))
    send_as = os.environ.get("MS365_SEND_AS", "").strip().lower()
    if send_as:
        allowed.add(send_as)
    return allowed


def _send_recipient_allowed(addr: str) -> bool:
    """A bare addr-spec (no display name / list syntax) whose domain is an
    allowed domain or a subdomain of one."""
    import re as _re
    global _PLAIN_ADDR_RE
    if _PLAIN_ADDR_RE is None:
        _PLAIN_ADDR_RE = _re.compile(
            r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})$")
    m = _PLAIN_ADDR_RE.match((addr or "").strip())
    if not m:
        return False
    dom = m.group(1).lower()
    return any(dom == d or dom.endswith("." + d) for d in _send_allowed_domains())


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
          "from": "jd@hhbagels.com",               # optional; honoured only if
                                                   #   MS365_SEND_AS or listed in
                                                   #   EMAIL_SEND_ALLOWED_FROM
          "reply_to_message_id": "<graph id>",     # optional; reply in-thread
          "save_to_sent_items": true,              # optional, default true
          "dry_run": false                         # optional; build, don't send
        }

    Always returns a structured 200 (never a bare 500):
      {"ok": true, "sent": true, ...} or {"ok": false, "error": "..."} --
    except a 400 {"ok": false, "refused": [...]} when any to/cc address is not
    a plain address in an allowed domain (EMAIL_SEND_ALLOWED_DOMAINS; default
    usfoods.com, cheneybrothers.com, chefswarehouse.com, onelineage.com,
    hhbagels.com). "from_overridden": true says a requested 'from' was not
    allowed and the default sender was used.
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
        # Recipients must be plain addresses in an allowed domain (the
        # distributors, Lineage, ourselves). Anyone holding the API token or
        # the shared login used to be able to mail ANY address -- with HTML,
        # from our domain (review 2026-10-22).
        refused = [a for a in to + cc if not _send_recipient_allowed(a)]
        if refused:
            return jsonify({
                "ok": False,
                "error": "recipient(s) not allowed: only plain addresses in "
                         + ", ".join(_send_allowed_domains())
                         + " (set EMAIL_SEND_ALLOWED_DOMAINS to change)",
                "refused": refused,
            }), 400

        subject = (body.get("subject") or "").strip()
        html = body.get("html")
        if html:
            content_type, content = "HTML", html
        else:
            content_type, content = "Text", (body.get("body") or "")

        dry_run = bool(body.get("dry_run", False))
        save_to_sent = bool(body.get("save_to_sent_items", True))
        reply_to = (body.get("reply_to_message_id") or "").strip()

        # Sender is PINNED: a body 'from' is honoured only when it is
        # MS365_SEND_AS or listed in EMAIL_SEND_ALLOWED_FROM; anything else
        # sends as the default (MS365_SEND_AS, else the first MS365_USER).
        # The app-only Mail.Send token can send as ANY tenant mailbox, so a
        # caller-chosen 'from' could impersonate anyone @hhbagels.com.
        sender = os.environ.get("MS365_SEND_AS", "").strip()
        if not sender:
            users = [u.strip() for u in os.environ.get("MS365_USER", "").split(",") if u.strip()]
            sender = users[0] if users else ""
        requested_from = str(body.get("from") or "").strip()
        from_overridden = False
        if requested_from:
            if requested_from.lower() in _send_allowed_from():
                sender = requested_from
            else:
                from_overridden = True
        if not sender:
            return jsonify({"ok": False, "error": "no sender configured (set MS365_SEND_AS or MS365_USER, or pass 'from')"}), 200

        if dry_run:
            return jsonify({"ok": True, "sent": False, "dry_run": True,
                            "from": sender, "from_overridden": from_overridden,
                            "to": to, "cc": cc,
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
                        "from": sender, "from_overridden": from_overridden,
                        "to": to, "cc": cc,
                        "subject": subject, "threaded": bool(reply_to)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc),
                        "traceback": ""}), 200


@email_bp.route("/api/email/cheney-stock-images", methods=["GET"])
def api_cheney_stock_images():
    """Serve Michael Ross's Cheney on-hand STOCK images straight from the
    mailbox, so a scheduled task can OCR them without needing the raw email
    attachments (the mail connector won't hand over binary xlsx headlessly).

    Ross pastes on-hand counts as an image in each weekly per-facility
    "Usage&Stock" .xlsx; the app's Graph credentials let the server fetch those
    attachments, pull out the embedded image + the report's period-end date, and
    return them base64-encoded. This stays lightweight (zip + openpyxl, already
    deps) -- the OCR itself runs client-side in the scheduled task, off this web
    service. Read-only against the mailbox.

    Query: lookback_days (default 9), sender (default mross@cheneybrothers.com).
    Returns {ok, facilities:[{warehouse, filename, count_date, received,
    image_content_type, image_b64}], errors:[...]}.
    """
    import base64
    import json as _json
    import urllib.parse
    import urllib.request
    from datetime import timezone
    try:
        if not _token_header_ok():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from integrations.email_scanner import EmailInboxClient, GRAPH_BASE
        from integrations.cheney_inventory_report import extract_stock_image
        try:
            lookback = int(request.args.get("lookback_days") or 9)
        except (TypeError, ValueError):
            lookback = 9
        lookback = max(1, min(lookback, 60))
        sender = (request.args.get("sender") or "mross@cheneybrothers.com").strip().lower()

        client = EmailInboxClient()
        if not client._has_ms365_credentials():
            return jsonify({"ok": False, "error": "MS365 not configured",
                            "facilities": []}), 200
        token = client._ms365_token()
        since = (datetime.now(timezone.utc) - timedelta(days=lookback)) \
            .replace(microsecond=0).isoformat().replace("+00:00", "Z")
        users = [u.strip() for u in os.environ.get("MS365_USER", "").split(",") if u.strip()]

        # Target Ross directly (avoids the general scan's problem of PO
        # confirmations crowding out the report). $orderby is on the same
        # property that is range-filtered, so Graph won't reject it.
        # Target Ross directly with $search (KQL). A from-address $filter
        # combined with a date range trips Graph's "InefficientFilter"; $search
        # avoids that. $search needs the ConsistencyLevel: eventual header and
        # can't combine with $filter/$orderby, so date + attachments are
        # filtered in code below.
        search = urllib.parse.quote(f'"from:{sender}"')
        facilities, errors = {}, []
        for user in users:
            uq = urllib.parse.quote(user)
            list_url = (f"{GRAPH_BASE}/users/{uq}/messages?$search={search}"
                        f"&$select=id,subject,receivedDateTime,hasAttachments,from"
                        f"&$top=25")
            try:
                _req = urllib.request.Request(list_url, method="GET", headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "ConsistencyLevel": "eventual"})
                with urllib.request.urlopen(_req, timeout=30) as _r:
                    msgs = _json.loads(_r.read()).get("value", [])
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{user}: list: {_safe_err(exc)}")
                continue
            for m in msgs:
                if not m.get("hasAttachments"):
                    continue
                # KQL "from:" also matches DISPLAY NAMES, so a stranger named
                # "mross@cheneybrothers.com" would be OCR'd into Cheney
                # on-hand. Graph's from address is authoritative (as in
                # /api/email/usf-screenshots).
                frm = (((m.get("from") or {}).get("emailAddress") or {})
                       .get("address") or "").strip().lower()
                if frm != sender:
                    continue
                mid = m.get("id") or ""
                recv = m.get("receivedDateTime", "") or ""
                if recv and recv < since:   # older than the lookback window
                    continue
                # No $select: contentBytes lives on the fileAttachment subtype,
                # not the base attachment type, so selecting it 400s. The full
                # listing returns contentBytes for file attachments by default.
                aurl = (f"{GRAPH_BASE}/users/{uq}/messages/{urllib.parse.quote(mid)}"
                        f"/attachments")
                try:
                    raw2, _ = client._graph_get(aurl, token)
                    atts = _json.loads(raw2).get("value", [])
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{user}/{mid[:8]}: att: {_safe_err(exc)}")
                    continue
                for a in atts:
                    name = a.get("name", "") or ""
                    if not name.lower().endswith(".xlsx"):
                        continue
                    cb = a.get("contentBytes")
                    if not cb:
                        continue
                    try:
                        wh, cd, img, ctype = extract_stock_image(base64.b64decode(cb), name)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{name}: parse: {_safe_err(exc)}")
                        continue
                    if not wh or not img:
                        continue
                    prev = facilities.get(wh)
                    if prev is None or recv > prev["received"]:
                        facilities[wh] = {
                            "warehouse": wh, "filename": name, "count_date": cd,
                            "received": recv, "image_content_type": ctype,
                            "image_b64": base64.b64encode(img).decode("ascii"),
                        }
        return jsonify({"ok": True, "lookback_days": lookback, "sender": sender,
                        "facilities": list(facilities.values()), "errors": errors})
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "cheney-stock-images")
        return jsonify({"ok": False, "error": _safe_err(exc), "facilities": []}), 200


def _graph_json(url, token, extra_headers=None, timeout=30):
    """GET a Microsoft Graph URL as JSON (module-level so tests can stub it)."""
    import json as _json
    import urllib.request
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    headers.update(extra_headers or {})
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read())


def _graph_token():
    from integrations.email_scanner import EmailInboxClient
    client = EmailInboxClient()
    if not client._has_ms365_credentials():
        return None
    return client._ms365_token()


def _screenshot_hashes_path():
    from inventory_tracker import DATA_DIR
    return DATA_DIR / "usf_screenshot_hashes.json"


def _load_screenshot_hashes() -> dict:
    """{sha256: {message_id, recorded_at}} of buyer-screen pictures already
    ingested as Houston counts (written by /api/email/ingest-events)."""
    from inventory_tracker import _read_json
    data = _read_json(_screenshot_hashes_path(), {})
    return data if isinstance(data, dict) else {}


def _record_screenshot_hashes(raw_events: list) -> int:
    """Remember the picture hash carried by ingested screenshot events, so the
    same screen re-sent (or carried along on a reply) is never a new count."""
    new = {}
    for e in raw_events or []:
        if not isinstance(e, dict):
            continue
        sha = str(e.get("image_sha256") or "").strip().lower()
        if len(sha) == 64 and all(c in "0123456789abcdef" for c in sha):
            new.setdefault(sha, str(e.get("source_message_id") or ""))
    if not new:
        return 0
    from inventory_tracker import _write_json, data_lock
    with data_lock():
        known = _load_screenshot_hashes()
        added = 0
        for sha, mid in new.items():
            if sha not in known:
                known[sha] = {"message_id": mid,
                              "recorded_at": datetime.now().isoformat()}
                added += 1
        if added:
            # Newest 500 is years of weekly screens.
            if len(known) > 500:
                keep = sorted(known.items(),
                              key=lambda kv: kv[1].get("recorded_at") or "")[-500:]
                known = dict(keep)
            _write_json(_screenshot_hashes_path(), known)
    return added


@email_bp.route("/api/email/usf-screenshots", methods=["GET"])
def api_usf_screenshots():
    """Serve a screenshot rep's recent replies (Houston: Tom Foley) so the OCR
    reader can turn them into counts -- see scripts/usf_screenshot_ocr.py.

    Tom's weekly numbers are an INLINE picture of his USF buyer screen plus
    usage typed in the body. The mailbox scan can't read a picture and the web
    service deliberately carries no OCR engine, so this route only fetches:
    each message from the warehouse's mapped reps in the lookback window, its
    plain-text body, and its pictures (>= 40 KB, so signature logos are left
    out). Graph reports inline-only mail as hasAttachments=false, so the
    attachments are fetched for every message rather than filtered on that.

    A message is "pending" when it was SENT after the warehouse's latest
    count: once its count is applied (stamped with that send time) it stops
    being pending, which is what makes the 6-hourly cron run a no-op the rest
    of the week.

    Query: warehouse (default "Houston, TX"), lookback_days (default 10,
    1-30), pending_only (default 1), meta_only (default 0: include image
    bytes base64; 1: names and sizes only -- the cron's cheap pre-check).
    Returns {ok, warehouse, last_count_at, messages:[{id, message_id, mailbox,
    sender, subject, sent, received, pending, body_text, images:[{name,
    content_type, size, b64?}]}], errors}.
    """
    import urllib.parse
    from datetime import timezone
    try:
        if not _token_header_ok():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from integrations.email_scanner import GRAPH_BASE
        import base64
        import hashlib
        from integrations.usf_screenshot import (
            MIN_SCREEN_IMAGE_BYTES, SCREENSHOT_DIST_BY_WAREHOUSE,
            cids_above_quote, normalize_cid, senders_for_warehouse,
        )
        from inventory_tracker import _parse_iso_utc, warehouse_freshness

        warehouse = (request.args.get("warehouse") or "Houston, TX").strip()
        if warehouse not in SCREENSHOT_DIST_BY_WAREHOUSE:
            return jsonify({"ok": False, "messages": [],
                            "error": f"{warehouse!r} is not a screenshot warehouse"}), 400
        try:
            lookback = int(request.args.get("lookback_days") or 10)
        except (TypeError, ValueError):
            lookback = 10
        lookback = max(1, min(lookback, 30))
        pending_only = (request.args.get("pending_only") or "1") != "0"
        meta_only = (request.args.get("meta_only") or "0") == "1"

        last_count = next((r.get("last_count_at") for r in warehouse_freshness()
                           if r.get("warehouse") == warehouse), None) or ""
        last_dt = _parse_iso_utc(last_count)

        token = _graph_token()
        if not token:
            return jsonify({"ok": False, "error": "MS365 not configured",
                            "messages": []}), 200
        since = datetime.now(timezone.utc) - timedelta(days=lookback)
        users = [u.strip() for u in os.environ.get("MS365_USER", "").split(",")
                 if u.strip()]

        found, errors = {}, []
        for user in users:
            uq = urllib.parse.quote(user)
            for sender in senders_for_warehouse(warehouse):
                # $search (KQL): a from-address $filter plus a date range
                # trips Graph's InefficientFilter. Dates are checked below.
                search = urllib.parse.quote(f'"from:{sender}"')
                url = (f"{GRAPH_BASE}/users/{uq}/messages?$search={search}"
                       "&$select=id,subject,sentDateTime,receivedDateTime,"
                       "internetMessageId,from&$top=25")
                try:
                    msgs = _graph_json(url, token, {"ConsistencyLevel": "eventual"}
                                       ).get("value", [])
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{user}/{sender}: list: {_safe_err(exc)}")
                    continue
                for m in msgs:
                    sent_dt = _parse_iso_utc(m.get("sentDateTime") or "")
                    if sent_dt is None or sent_dt < since:
                        continue
                    frm = (((m.get("from") or {}).get("emailAddress") or {})
                           .get("address") or "").lower()
                    if frm != sender:
                        continue   # KQL from: also matches display names
                    pending = last_dt is None or sent_dt > last_dt
                    if pending_only and not pending:
                        continue
                    key = m.get("internetMessageId") or m.get("id")
                    if key in found:
                        continue   # same mail delivered to JD@ and info@
                    found[key] = {"user": user, "graph": m, "pending": pending}

        out = []
        seen_hashes = _load_screenshot_hashes()
        for key, f in found.items():
            m, uq = f["graph"], urllib.parse.quote(f["user"])
            mid = urllib.parse.quote(m.get("id") or "")
            base = f"{GRAPH_BASE}/users/{uq}/messages/{mid}"
            body_text, images, rejected = "", [], []
            try:
                body = _graph_json(base + "?$select=body", token,
                                   {"Prefer": 'outlook.body-content-type="text"'})
                body_text = ((body.get("body") or {}).get("content") or "")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{key}: body: {_safe_err(exc)}")
            # The HTML body says which inline pictures sit in the reply's OWN
            # part; anything else is a quoted-thread copy (see
            # usf_screenshot.cids_above_quote). No HTML -> no picture counts.
            own_cids = set()
            try:
                hbody = _graph_json(base + "?$select=body", token,
                                    {"Prefer": 'outlook.body-content-type="html"'})
                own_cids = cids_above_quote(
                    ((hbody.get("body") or {}).get("content") or ""))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{key}: html body: {_safe_err(exc)}")
            try:
                att_url = base + "/attachments"
                if meta_only:
                    # contentId lives on the fileAttachment subtype, so it
                    # can't be $selected here: the cheap pre-check can only
                    # tell "no picture in the reply's own part" (-> none).
                    att_url += "?$select=name,contentType,size,isInline"
                for a in _graph_json(att_url, token).get("value", []):
                    ctype = (a.get("contentType") or "").lower()
                    if not ctype.startswith("image/"):
                        continue
                    if int(a.get("size") or 0) < MIN_SCREEN_IMAGE_BYTES:
                        continue
                    img = {"name": a.get("name") or "", "content_type": ctype,
                           "size": int(a.get("size") or 0)}
                    why = None
                    if not a.get("isInline"):
                        why = "not an inline picture in the reply"
                    elif not own_cids:
                        why = "no picture in the reply's own text (quoted thread only)"
                    elif not meta_only and normalize_cid(a.get("contentId")) not in own_cids:
                        why = "picture is in the quoted thread, not the reply"
                    if why is None and not meta_only:
                        if not a.get("contentBytes"):
                            continue
                        try:
                            sha = hashlib.sha256(
                                base64.b64decode(a["contentBytes"])).hexdigest()
                        except (ValueError, TypeError):
                            sha = ""
                        if sha and sha in seen_hashes:
                            why = "this exact picture was already ingested"
                        else:
                            img["b64"] = a["contentBytes"]
                            img["sha256"] = sha
                    if why:
                        rejected.append({**img, "reason": why})
                        continue
                    images.append(img)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{key}: attachments: {_safe_err(exc)}")
            out.append({
                "id": m.get("id"), "message_id": m.get("internetMessageId") or "",
                "mailbox": f["user"],
                "sender": (((m.get("from") or {}).get("emailAddress") or {})
                           .get("address") or "").lower(),
                "subject": m.get("subject") or "",
                "sent": m.get("sentDateTime") or "",
                "received": m.get("receivedDateTime") or "",
                "pending": f["pending"], "body_text": body_text,
                "images": images, "rejected_images": rejected,
            })
        out.sort(key=lambda r: r["sent"])
        return jsonify({"ok": True, "warehouse": warehouse,
                        "last_count_at": last_count or None,
                        "lookback_days": lookback, "pending_only": pending_only,
                        "messages": out, "errors": errors})
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "usf-screenshots")
        return jsonify({"ok": False, "error": _safe_err(exc), "messages": []}), 200


@email_bp.route("/api/email/ingest-events", methods=["POST"])
def api_email_ingest_events():
    with _ingest_lock() as got:
        if not got:
            return _busy_response("/api/email/ingest-events")
        return _api_email_ingest_events_locked()


def _api_email_ingest_events_locked():
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
        from core.util import _norm_po_key
        _canceled_keys = {_norm_po_key(k) for k in canceled}
        kept_events = []
        for ev in raw_events:
            po = str((ev or {}).get("po_number") or "").strip()
            if po and _norm_po_key(po) in _canceled_keys:
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
                source_received_at=str(e.get("source_received_at") or ""),
                source_sender=str(e.get("source_sender") or ""),
                count_date=str(e.get("count_date") or ""),
                parser_source=str(e.get("parser_source") or ""),
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

    if not dry_run:
        try:
            _record_screenshot_hashes(raw_events)
        except Exception as exc:  # noqa: BLE001 -- best-effort bookkeeping
            _log_exc(exc, "record screenshot hashes")
    return jsonify({"dry_run": dry_run, "reports": [report]})


@email_bp.route("/api/ingest/cheney-inventory-csv", methods=["POST"])
def api_ingest_cheney_inventory_csv():
    """Ingest a Cheney Brothers daily on-hand CSV (the SFTP feed) over HTTPS.

    Render can't host an SFTP server, so this is the receiving side of the
    feed: whatever picks up Cheney's daily drop (a scheduled Cowork routine, a
    small relay, or a manual upload) POSTs the CSV here and it lands through
    the same apply pipeline as the mailbox scan. Accepts the CSV as the raw
    request body (text/csv), a multipart ``file`` field, or JSON {"csv": ...}.
    Gated by the global auth hook (X-Inventory-Token / session). Pass
    ``?dry_run=1`` to parse + report without applying. source="cheney-sftp-csv".
    """
    try:
        from integrations import EmailEvent, SyncItem
        from integrations.cheney_csv_inventory import parse_inventory_csv
        from sync_inventory import _apply_events
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc, "import")}), 500

    dry_run = str(request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
    filename = request.args.get("filename") or "cheney_inventory.csv"
    csv_text = ""
    up = request.files.get("file") if request.files else None
    if up is not None:
        csv_text = up.read().decode("utf-8-sig", "replace")
    elif request.is_json:
        body = request.get_json(silent=True) or {}
        csv_text = str(body.get("csv") or "")
        if str(body.get("dry_run")).lower() in ("1", "true", "yes"):
            dry_run = True
        if body.get("filename"):
            filename = str(body["filename"])
    else:
        csv_text = request.get_data(as_text=True) or ""

    events_d, errors = parse_inventory_csv(csv_text, filename=filename)
    built = []
    for e in events_d:
        it = e.get("item") or {}
        built.append(EmailEvent(
            event_type=e.get("event_type") or "on_hand",
            item=SyncItem(
                quantity=float(it.get("quantity") or 0),
                distributor=str(it.get("distributor") or ""),
                name=it.get("name"),
                variety=it.get("variety"),
                warehouse=it.get("warehouse"),
                unit=it.get("unit"),
                distributor_sku=it.get("distributor_sku"),
                case_cost=(float(it["case_cost"]) if it.get("case_cost") is not None else None),
                case_size=(int(it["case_size"]) if it.get("case_size") is not None else None),
                weekly_usage=(float(it["weekly_usage"]) if it.get("weekly_usage") is not None else None),
            ),
            source_message_id=str(e.get("source_message_id") or ""),
            source_subject=str(e.get("source_subject") or ""),
            po_number="",
            po_revision="",
            count_date=str(e.get("count_date") or ""),
        ))

    try:
        with _ingest_lock() as got:
            if not got:
                return jsonify({"ok": False,
                                "error": "another ingest is running"}), 409
            report = _apply_events(
                events=built, messages_seen=1, messages_parsed=1 if built else 0,
                errors=list(errors), dry_run=dry_run, source="cheney-sftp-csv",
            )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"dry_run": dry_run, "reports": [{
            "distributor": "Cheney Brothers", "source": "cheney-sftp-csv",
            "status": "error", "fetched": len(built), "updated": 0,
            "unchanged": 0, "unmatched": [], "changes": [],
            "error": _safe_err(exc), "messages_seen": 1, "messages_parsed": 1,
        }]}), 500

    return jsonify({"dry_run": dry_run, "reports": [report]})
