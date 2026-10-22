"""Lineage delivery tracking routes -- scheduled ETA, changes, confirmed arrival.

The free-text reading of Lineage's replies happens in the "Lineage PO delivery
check" scheduled task (a cloud task since 10/15 -- no laptop -- holding only
the limited DELIVERY_API_TOKEN); these routes are what it talks to:

  POST /api/pos/delivery/sync-threads    fetch the "Quote and Schedule" threads
                                         (server-side Graph), register each open
                                         PO they list, return the threads + the
                                         messages to read; caches what it read
  POST /api/pos/delivery/events          apply what the task read (scheduled /
                                         rescheduled / delivered), each with its
                                         evidence -- which must quote a message
                                         the sync actually read, from Lineage or
                                         the DC; moves the PO's dates and, on
                                         delivery, releases it into on-hand
  POST /api/pos/delivery/send-followups  the SERVER writes and sends the due
                                         follow-ups (fixed recipients/wording),
                                         threaded, and records them
  POST /api/pos/delivery/chased          record a follow-up sent by hand
  GET  /api/pos/delivery                 tracked POs + what follow-up is due

Rules live in integrations/po_delivery.py; the rollover hold in
inventory_tracker._delivery_gate.
"""
import json
import os
import urllib.parse
from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request

from core.errors import _log_exc, _safe_err
from core.util import with_data_lock

delivery_bp = Blueprint("delivery", __name__)

THREAD_SUBJECT = "Quote and Schedule"
# Lineage also answers on one-off threads ("H&H Bagels PO Delivery Status",
# "H&H Bagels PO 3795383T Delivery Status", "... PO Delivery - PO 0145...").
# Those are read as EVIDENCE only -- their delivery confirmations can be
# quoted (lead times, history), but nothing is ever registered or chased on
# them and no follow-up is ever sent on them.
EVIDENCE_SUBJECT = "Delivery"
MESSAGE_CACHE_DAYS = 120


def _now():
    return datetime.now(timezone.utc)


def _scope() -> str:
    """'delivery' when the caller holds only the limited DELIVERY_API_TOKEN
    (set by the app's auth gate), else 'full' (session or the main token)."""
    return getattr(g, "auth_scope", "") or "full"


def _ledger_by_po() -> dict:
    from blueprints.pos import build_po_ledger
    from integrations.po_delivery import norm_po
    return {norm_po(r.get("po_number") or ""): r for r in build_po_ledger()}


def _is_open(ledger_rec, trk) -> bool:
    """Still worth following up: on order (not arrived / canceled), or tracked
    and not yet confirmed delivered."""
    from integrations.po_delivery import HOLD_STATUSES
    if trk and trk.get("status") in HOLD_STATUSES:
        return True
    if ledger_rec is None:
        return False
    return (ledger_rec.get("status") or "") not in ("arrived", "canceled", "cancelled")


def _apply_to_inventory(po_key: str, rec: dict) -> dict:
    """Mirror a tracking change onto the PO's pending rows (dates shown on the
    Pending tab). The hold / release itself is decided by the rollover gate,
    which reads the tracking file -- load_inventory() below already applies it,
    so a confirmed delivery rolls into on-hand right here."""
    from inventory_tracker import load_inventory, save_inventory
    from integrations.po_delivery import norm_po, wall_time
    inv = load_inventory()
    updated = 0
    items = []
    st = rec.get("status")
    for key, item in inv.items():
        for e in (item.get("on_order") or []):
            if norm_po(e.get("po_number") or "") != po_key:
                continue
            if (e.get("arrival_source") or "") == "operator":
                continue          # a human's explicit arrival wins
            before = dict(e)
            if st == "scheduled" and rec.get("delivery_appt"):
                local = wall_time(rec["delivery_appt"])
                e["arrival_date"] = local
                e["eta"] = local
                e["arrival_source"] = "lineage"
                if rec.get("pickup_at") and e.get("ship_date_source") != "freight":
                    e["ship_date"] = wall_time(rec["pickup_at"])
                    e["ship_date_source"] = "lineage"
            elif st == "delivered" and rec.get("delivered_at"):
                local = wall_time(rec["delivered_at"])
                e["arrival_date"] = local
                e["eta"] = local
                e["arrival_source"] = "lineage-delivered"
            if e != before:
                updated += 1
                if item.get("name") not in items:
                    items.append(item.get("name") or key)
    if updated:
        save_inventory(inv)
    return {"entries_updated": updated, "items": items}


@delivery_bp.route("/api/pos/delivery", methods=["GET"])
def api_delivery_list():
    from inventory_tracker import load_po_delivery
    from integrations.po_delivery import summary
    try:
        show_all = (request.args.get("all") or "0") == "1"
        tracking = load_po_delivery()
        ledger = _ledger_by_po()
        now = _now()
        out = []
        for key, rec in tracking.items():
            lr = ledger.get(key)
            if not show_all and not _is_open(lr, rec):
                continue
            out.append({**summary(rec, now), "po_number": rec.get("po_number") or key,
                        "ledger_status": (lr or {}).get("status", "not_in_tracker"),
                        "distributor": (lr or {}).get("distributor", ""),
                        "warehouse": (lr or {}).get("warehouse", ""),
                        "total_cs": (lr or {}).get("total_cs", 0),
                        "thread": rec.get("thread") or {}})
        out.sort(key=lambda r: (r["status"], r["delivery_appt"] or r["requested_at"]))
        return jsonify({"ok": True, "count": len(out), "pos": out})
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "delivery-list")
        return jsonify({"ok": False, "error": _safe_err(exc), "pos": []}), 200


def _is_evidence_subject(subject: str) -> bool:
    """A one-off Lineage delivery thread about our POs (not Quote and Schedule)."""
    import re as _re
    s = (subject or "").lower()
    return ("delivery" in s and THREAD_SUBJECT.lower() not in s
            and bool(_re.search(r"\bpo\b|po#|h&h", s)))


def _fetch_thread_messages(lookback_days: int, subject: str = THREAD_SUBJECT,
                           keep=None) -> tuple:
    """Every message whose subject matches ``subject`` (default: the "Quote
    and Schedule" threads) in the mailboxes, deduped across them (preferring
    JD's copy, whose Graph id a threaded reply needs). ``keep`` filters by
    subject; the default keeps subjects containing ``subject``."""
    from blueprints.email import _graph_json, _graph_token
    from integrations.email_scanner import GRAPH_BASE
    from integrations.po_delivery import clean_body, parse_dt
    token = _graph_token()
    if not token:
        raise RuntimeError("MS365 not configured")
    since = _now() - timedelta(days=lookback_days)
    users = [u.strip() for u in os.environ.get("MS365_USER", "").split(",") if u.strip()]
    users.sort(key=lambda u: 0 if u.lower().startswith("jd@") else 1)
    found, errors = {}, []
    search = urllib.parse.quote(f'"subject:{subject}"')
    if keep is None:
        keep = lambda subj: subject.lower() in (subj or "").lower()  # noqa: E731
    for user in users:
        url = (f"{GRAPH_BASE}/users/{urllib.parse.quote(user)}/messages?"
               f"$search={search}&$top=100&$select=id,subject,conversationId,"
               "sentDateTime,from,toRecipients,ccRecipients,internetMessageId,"
               "uniqueBody")
        pages = 0
        while url and pages < 6:
            pages += 1
            try:
                page = _graph_json(url, token, {
                    "ConsistencyLevel": "eventual",
                    "Prefer": 'outlook.body-content-type="text"'}, timeout=60)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{user}: list: {_safe_err(exc)}")
                break
            for m in page.get("value", []):
                if not keep(m.get("subject") or ""):
                    continue
                sent = parse_dt(m.get("sentDateTime") or "")
                if sent is None or sent < since:
                    continue
                key = m.get("internetMessageId") or m.get("id")
                if key in found:
                    continue
                addr = lambda r: ((r or {}).get("emailAddress") or {}).get("address", "").lower()
                found[key] = {
                    "graph_id": m.get("id"), "mailbox": user,
                    "message_id": m.get("internetMessageId") or "",
                    "conversation_id": m.get("conversationId") or "",
                    "subject": m.get("subject") or "",
                    "sent": m.get("sentDateTime") or "",
                    "from": addr(m.get("from")),
                    "to": [addr(r) for r in m.get("toRecipients") or []],
                    "cc": [addr(r) for r in m.get("ccRecipients") or []],
                    # full text: a long request lists every PO block
                    "text": clean_body(((m.get("uniqueBody") or {}).get("content") or ""),
                                       limit=20000),
                }
            url = page.get("@odata.nextLink")
    return list(found.values()), errors


@delivery_bp.route("/api/pos/delivery/sync-threads", methods=["POST"])
@with_data_lock
def api_delivery_sync_threads():
    """Fetch the Quote and Schedule threads, register each open PO they list
    (status "requested"), and return the threads that still have an open PO,
    with their messages oldest-first and the id to reply on."""
    from inventory_tracker import (
        load_po_delivery, load_po_delivery_threads, save_po_delivery,
        save_po_delivery_threads,
    )
    from integrations.po_delivery import (
        apply_event, is_hh, norm_po, parse_dt, parse_request_blocks,
        sender_may_report, summary,
    )
    try:
        body = request.get_json(silent=True) or {}
        lookback = max(3, min(int(body.get("lookback_days") or 45), 120))
        dry_run = bool(body.get("dry_run", False))
        msgs, errors = _fetch_thread_messages(lookback)
        convs: dict = {}
        for m in msgs:
            convs.setdefault(m["conversation_id"], []).append(m)
        try:
            ev_msgs, ev_errors = _fetch_thread_messages(
                lookback, subject=EVIDENCE_SUBJECT, keep=_is_evidence_subject)
        except Exception as exc:  # noqa: BLE001 -- evidence is optional
            ev_msgs, ev_errors = [], [f"evidence threads: {_safe_err(exc)}"]
        ev_convs: dict = {}
        for m in ev_msgs:
            if m["conversation_id"] not in convs:
                ev_convs.setdefault(m["conversation_id"], []).append(m)
        tracking = load_po_delivery()
        ledger = _ledger_by_po()
        now = _now()
        # What this read saw -- the events route checks quotes against it and
        # send-followups replies off it. A mirror of the mailbox, not tracker
        # state, so it is refreshed even on a dry run.
        cache = load_po_delivery_threads() or {}
        cutoff = now - timedelta(days=MESSAGE_CACHE_DAYS)
        cached_msgs = {k: v for k, v in (cache.get("messages") or {}).items()
                       if (parse_dt(v.get("sent") or "") or now) >= cutoff}
        cache_threads = {}
        threads, registered, not_in_tracker = [], [], []
        for conv, ms in convs.items():
            ms.sort(key=lambda m: m["sent"])
            for m in ms:
                if m["message_id"]:
                    cached_msgs[m["message_id"]] = {
                        k: m[k] for k in ("from", "sent", "conversation_id",
                                          "subject", "text", "graph_id", "mailbox")}
            requested = []
            for m in ms:
                if not is_hh(m["from"]):
                    continue
                for blk in parse_request_blocks(m["text"]):
                    if norm_po(blk["po_number"]) not in {norm_po(b["po_number"]) for b in requested}:
                        requested.append({**blk, "request_message_id": m["message_id"],
                                          "request_sent": m["sent"]})
            if not requested:
                continue
            jd_copy = next((m for m in reversed(ms)
                            if m["mailbox"].lower().startswith("jd@")), None)
            cache_threads[conv] = {
                "conversation_id": conv, "subject": ms[0]["subject"],
                "reply_to_graph_id": (jd_copy or {}).get("graph_id", ""),
                "reply_subject": (jd_copy or ms[-1])["subject"],
                "last_sent": ms[-1]["sent"], "last_from": ms[-1]["from"],
                "po_numbers": [norm_po(b["po_number"]) for b in requested],
            }
            open_pos = []
            for blk in requested:
                key = norm_po(blk["po_number"])
                lr, trk = ledger.get(key), tracking.get(key)
                if lr is None and trk is None:
                    not_in_tracker.append(blk["po_number"])
                    continue
                if not _is_open(lr, trk):
                    continue
                if trk is None:
                    rec, change, err = apply_event(None, {
                        "po_number": (lr or {}).get("po_number") or blk["po_number"],
                        "event": "requested",
                        "thread": {"conversation_id": conv, "subject": ms[0]["subject"]},
                        "request": {k: blk[k] for k in ("quantity", "address",
                                                        "receiving_hours", "invoice")},
                        "evidence": {"message_id": blk["request_message_id"],
                                     "sender": "", "sent": blk["request_sent"],
                                     "quote": f"PO #: {blk['po_number']}"}}, now)
                    if not err and change:
                        tracking[key] = rec
                        registered.append(rec["po_number"])
                    trk = tracking.get(key)
                open_pos.append({
                    "po_number": (lr or {}).get("po_number") or blk["po_number"],
                    "distributor": (lr or {}).get("distributor", ""),
                    "warehouse": (lr or {}).get("warehouse", ""),
                    "total_cs": (lr or {}).get("total_cs", 0),
                    "ledger_status": (lr or {}).get("status", ""),
                    "destination": blk.get("address", ""),
                    "quantity": blk.get("quantity", ""),
                    "receiving_hours": blk.get("receiving_hours", ""),
                    "tracking": summary(trk, now) if trk else None,
                })
            if not open_pos:
                continue
            reply_to = next((m for m in reversed(ms)
                             if m["mailbox"].lower().startswith("jd@")), ms[-1])
            threads.append({
                "conversation_id": conv, "subject": ms[0]["subject"],
                "reply_to_graph_id": reply_to["graph_id"],
                "reply_subject": reply_to["subject"],
                "open_pos": open_pos,
                "messages": [{**{k: m[k] for k in ("message_id", "sent", "from",
                                                    "to", "cc")},
                              "text": m["text"][:1500]} for m in ms],
            })
        # Evidence-only threads: cache what Lineage wrote, with the POs each
        # thread names (ledger PO numbers found in its subject / messages), so
        # a delivery confirmation there can be quoted. Never registered,
        # never chased, never replied on (send-followups skips them).
        import re as _re
        evidence_threads = []
        for conv, ms in ev_convs.items():
            if not any(sender_may_report(m["from"]) for m in ms):
                continue
            ms.sort(key=lambda m: m["sent"])
            blob = " ".join([ms[0]["subject"]] + [m["text"] for m in ms]).upper()
            pos = sorted({t for t in _re.findall(r"[A-Z0-9]{5,14}", blob)
                          if t in ledger or t in tracking})
            if not pos:
                continue
            for m in ms:
                if m["message_id"]:
                    cached_msgs[m["message_id"]] = {
                        k: m[k] for k in ("from", "sent", "conversation_id",
                                          "subject", "text", "graph_id", "mailbox")}
            cache_threads[conv] = {
                "conversation_id": conv, "subject": ms[0]["subject"],
                "reply_to_graph_id": "", "reply_subject": ms[-1]["subject"],
                "last_sent": ms[-1]["sent"], "last_from": ms[-1]["from"],
                "po_numbers": pos, "evidence_only": True,
            }
            evidence_threads.append({"conversation_id": conv,
                                     "subject": ms[0]["subject"],
                                     "po_numbers": pos, "messages": len(ms)})
        # ev_errors stay out of `errors`: the cache's errors gate follow-ups,
        # and an unread evidence thread can't change whether one is due.
        if registered and not dry_run:
            save_po_delivery(tracking)
        # A partial read is still cached (its messages are real, so quotes can
        # be checked against them) but marked, and send-followups won't chase
        # off it: a reply in the mailbox we couldn't read might answer the ask.
        save_po_delivery_threads({"synced_at": now.isoformat(),
                                  "lookback_days": lookback,
                                  "errors": list(errors),
                                  "messages": cached_msgs,
                                  "threads": cache_threads})
        threads.sort(key=lambda t: t["messages"][-1]["sent"], reverse=True)
        return jsonify({"ok": True, "lookback_days": lookback, "dry_run": dry_run,
                        "threads": threads, "registered": registered,
                        "not_in_tracker": sorted(set(not_in_tracker)),
                        "evidence_threads": evidence_threads,
                        "evidence_errors": ev_errors,
                        "errors": errors})
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "delivery-sync-threads")
        return jsonify({"ok": False, "error": _safe_err(exc), "threads": []}), 200


def _verify_evidence(ev: dict, po_key: str, cache: dict) -> tuple:
    """Check an event's evidence against what the last sync read from the
    mailbox. Returns (event, None) with sender / sent replaced by the
    mailbox's own values, or (event, reason) when it can't be trusted: the
    message wasn't read, its thread doesn't list this PO, or the quote isn't in
    it. (The sender rule -- Lineage or the DC only -- is apply_event's.)"""
    from integrations.po_delivery import quote_in_text
    evid = dict(ev.get("evidence") or {})
    msg = (cache.get("messages") or {}).get(str(evid.get("message_id") or ""))
    if not msg:
        return ev, ("evidence.message_id isn't a message the last sync read "
                    "(run sync-threads, then quote a message from it)")
    th = (cache.get("threads") or {}).get(msg.get("conversation_id") or "") or {}
    if po_key not in (th.get("po_numbers") or []):
        return ev, "that message's thread doesn't list this PO"
    if not quote_in_text(str(evid.get("quote") or ""), msg.get("text") or ""):
        return ev, "evidence.quote isn't in that message (quote it word for word)"
    evid["sender"] = msg.get("from") or ""
    evid["sent"] = msg.get("sent") or evid.get("sent") or ""
    return {**ev, "evidence": evid}, None


@delivery_bp.route("/api/pos/delivery/events", methods=["POST"])
@with_data_lock
def api_delivery_events():
    """Apply events read off the threads. Body {events:[{po_number, event,
    delivery_appt?, pickup_at?, delivered_at?, confirmation_no?, evidence:
    {message_id, sender, sent, quote}}], dry_run}. Dates are ISO with the
    receiving dock's UTC offset. Each result says what moved, or why not."""
    from inventory_tracker import (
        load_po_delivery, load_po_delivery_threads, save_po_delivery,
    )
    from integrations.po_delivery import apply_event, norm_po, summary
    try:
        body = request.get_json(silent=True) or {}
        dry_run = bool(body.get("dry_run", False))
        events = body.get("events") or []
        if not isinstance(events, list):
            return jsonify({"ok": False, "error": "events must be a list"}), 400
        tracking = load_po_delivery()
        ledger = _ledger_by_po()
        cache = load_po_delivery_threads() or {}
        limited = _scope() == "delivery"
        now = _now()
        results, touched = [], []
        for ev in events:
            ev = dict(ev) if isinstance(ev, dict) else {}
            key = norm_po(ev.get("po_number") or "")
            lr = ledger.get(key)
            if lr is None and key not in tracking:
                results.append({"po_number": ev.get("po_number"), "applied": False,
                                "reason": "PO not in the tracker"})
                continue
            kind = str(ev.get("event") or "").strip().lower()
            if limited:
                ev.pop("force", None)
                if kind not in ("scheduled", "rescheduled", "delivered"):
                    results.append({"po_number": ev.get("po_number"), "applied": False,
                                    "reason": f"'{kind}' isn't allowed with the delivery key"})
                    continue
            if kind in ("scheduled", "rescheduled", "delivered"):
                ev, why = _verify_evidence(ev, key, cache)
                if why:
                    results.append({"po_number": ev.get("po_number"), "applied": False,
                                    "reason": why})
                    continue
            rec, change, err = apply_event(tracking.get(key), ev, now)
            if err:
                results.append({"po_number": ev.get("po_number"), "applied": False,
                                "reason": err})
                continue
            if change is None:
                results.append({"po_number": ev.get("po_number"), "applied": False,
                                "reason": "no change (already recorded)"})
                continue
            tracking[key] = rec
            touched.append(key)
            results.append({"po_number": rec["po_number"], "applied": True,
                            "change": change, "tracking": summary(rec, now),
                            "ledger_status_before": (lr or {}).get("status", "")})
        inventory = {}
        rederived = None
        if touched and not dry_run:
            save_po_delivery(tracking)
            for key in dict.fromkeys(touched):
                inventory[key] = _apply_to_inventory(key, tracking[key])
            if any(tracking[k].get("status") == "delivered" for k in touched):
                # A new confirmed delivery is a new lead-time sample: re-estimate
                # the arrivals still derived from a ship date.
                try:
                    from blueprints.freight import rederive_estimated_arrivals
                    rederived = rederive_estimated_arrivals(dry_run=False)
                except Exception as exc:  # noqa: BLE001 -- never fail the event
                    _log_exc(exc, "delivery-events rederive")
            after = _ledger_by_po()
            for r in results:
                if r.get("applied"):
                    k = norm_po(r["po_number"])
                    r["inventory"] = inventory.get(k, {})
                    r["ledger_status_after"] = (after.get(k) or {}).get("status", "")
        out = {"ok": True, "dry_run": dry_run, "results": results}
        if rederived is not None:
            out["arrivals_rederived"] = {k: rederived.get(k)
                                         for k in ("pos_changed", "changes")}
        return jsonify(out)
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "delivery-events")
        return jsonify({"ok": False, "error": _safe_err(exc), "results": []}), 200


@delivery_bp.route("/api/pos/delivery/chased", methods=["POST"])
@with_data_lock
def api_delivery_chased():
    """Record follow-ups sent: {po_numbers:[...], ask, message_id}."""
    from inventory_tracker import load_po_delivery, save_po_delivery
    from integrations.po_delivery import norm_po, record_chase
    try:
        body = request.get_json(silent=True) or {}
        ask = str(body.get("ask") or "").strip()
        if ask not in ("schedule", "confirm_delivery", "other"):
            return jsonify({"ok": False, "error": "ask must be schedule | confirm_delivery | other"}), 400
        tracking = load_po_delivery()
        now = _now()
        done, missing = [], []
        for po in body.get("po_numbers") or []:
            key = norm_po(po)
            if key not in tracking:
                missing.append(po)
                continue
            tracking[key] = record_chase(tracking[key], ask, now,
                                         str(body.get("message_id") or ""))
            done.append(tracking[key]["po_number"])
        if done and not body.get("dry_run"):
            save_po_delivery(tracking)
        return jsonify({"ok": True, "recorded": done, "not_tracked": missing})
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "delivery-chased")
        return jsonify({"ok": False, "error": _safe_err(exc)}), 200


def _send_reply(sender: str, reply_to_graph_id: str, to, cc, subject: str,
                body_text: str) -> str:
    """Reply in-thread from ``sender``'s mailbox (createReply -> patch -> send),
    the same Graph calls /api/email/send makes. Returns the draft id."""
    import urllib.request
    from core.http import _TRUSTED_OUTBOUND_HOSTS
    from integrations.email_scanner import GRAPH_BASE, EmailInboxClient
    token = EmailInboxClient()._ms365_token()
    uq = urllib.parse.quote

    def call(method, url, data=None):
        if (urllib.parse.urlparse(url).hostname or "").lower() not in _TRUSTED_OUTBOUND_HOSTS:
            raise ValueError("refusing outbound request to untrusted host")
        payload = None if data is None else json.dumps(data).encode("utf-8")
        headers = {"Authorization": f"Bearer {token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return raw.decode("utf-8", "replace") if raw else ""

    box = f"{GRAPH_BASE}/users/{uq(sender)}/messages"
    draft = json.loads(call("POST", f"{box}/{uq(reply_to_graph_id)}/createReply") or "{}")
    draft_id = draft.get("id")
    if not draft_id:
        raise RuntimeError("createReply returned no draft id")
    rc = lambda addrs: [{"emailAddress": {"address": a}} for a in addrs]
    call("PATCH", f"{box}/{uq(draft_id)}", {
        "subject": subject, "body": {"contentType": "Text", "content": body_text},
        "toRecipients": rc(to), "ccRecipients": rc(cc)})
    call("POST", f"{box}/{uq(draft_id)}/send")
    return draft_id


@delivery_bp.route("/api/pos/delivery/send-followups", methods=["POST"])
@with_data_lock
def api_delivery_send_followups():
    """Write and send the follow-ups that are due, one threaded email per
    Quote and Schedule thread, then record them. Body {dry_run}.

    Everything about the email is decided here, not by the caller: who is due
    (po_delivery.chase_due), the recipients (FOLLOWUP_TO / FOLLOWUP_CC), the
    wording (ASK_TEXT), and the thread (from the last sync). Guards: the sync
    must be fresh (SYNC_FRESH_FOR) and complete; a PO someone wrote about in
    the last THREAD_QUIET_FOR is held back (po_delivery.quiet_pos); at most
    MAX_FOLLOWUP_EMAILS."""
    from inventory_tracker import (
        load_po_delivery, load_po_delivery_threads, save_po_delivery,
    )
    from integrations.po_delivery import (
        FOLLOWUP_CC, FOLLOWUP_FROM, FOLLOWUP_TO, MAX_FOLLOWUP_EMAILS,
        SYNC_FRESH_FOR, chase_due, followup_body, parse_dt, quiet_pos,
        record_chase, reply_subject,
    )
    try:
        body = request.get_json(silent=True) or {}
        dry_run = bool(body.get("dry_run", False))
        now = _now()
        cache = load_po_delivery_threads() or {}
        synced = parse_dt(cache.get("synced_at") or "")
        if synced is None or now - synced > SYNC_FRESH_FOR:
            return jsonify({"ok": False, "error": "threads not synced in the last "
                            f"{int(SYNC_FRESH_FOR.total_seconds() // 60)} min -- "
                            "run sync-threads first", "emails": []}), 200
        if cache.get("errors"):
            return jsonify({"ok": False, "error": "the last sync couldn't read every "
                            "mailbox; not following up off a partial read",
                            "sync_errors": cache["errors"], "emails": []}), 200
        threads = cache.get("threads") or {}
        tracking = load_po_delivery()
        ledger = _ledger_by_po()

        groups, no_thread = {}, []
        for key, rec in tracking.items():
            lr = ledger.get(key)
            if not _is_open(lr, rec):
                continue
            ask = chase_due(rec, now)
            if not ask:
                continue
            conv = (rec.get("thread") or {}).get("conversation_id") or ""
            if conv not in threads or threads[conv].get("evidence_only"):
                conv = next((c for c, t in sorted(threads.items(),
                                                  key=lambda kv: kv[1].get("last_sent") or "",
                                                  reverse=True)
                             if key in (t.get("po_numbers") or [])
                             and not t.get("evidence_only")), "")
            if not conv:
                no_thread.append(rec.get("po_number") or key)
                continue
            groups.setdefault(conv, []).append({
                "key": key, "po_number": rec.get("po_number") or key, "ask": ask,
                "distributor": (lr or {}).get("distributor", ""),
                "warehouse": (lr or {}).get("warehouse", ""),
                "delivery_appt": rec.get("delivery_appt") or ""})

        emails, sent_any = [], False
        all_msgs = list((cache.get("messages") or {}).values())
        for conv, items in sorted(groups.items(),
                                  key=lambda kv: threads[kv[0]].get("last_sent") or ""):
            th = threads[conv]
            items.sort(key=lambda i: i["po_number"])
            quiet = quiet_pos([m for m in all_msgs if m.get("conversation_id") == conv],
                              th.get("po_numbers") or [], now)
            held = {i["po_number"]: quiet[i["key"]] for i in items if i["key"] in quiet}
            items = [i for i in items if i["key"] not in quiet]
            out = {"conversation_id": conv, "subject": reply_subject(th.get("reply_subject")
                                                                     or th.get("subject")),
                   "po_numbers": [i["po_number"] for i in items],
                   "asks": {i["po_number"]: i["ask"] for i in items},
                   "to": list(FOLLOWUP_TO), "cc": list(FOLLOWUP_CC),
                   "sent": False}
            if held:
                out["held_back"] = held
            if not items:
                out["skipped"] = "; ".join(sorted(set(held.values())))
                emails.append(out)
                continue
            if not th.get("reply_to_graph_id"):
                out["skipped"] = "no copy of this thread in JD's mailbox to reply on"
                emails.append(out)
                continue
            if len([e for e in emails if e.get("sent") or e.get("would_send")]) >= MAX_FOLLOWUP_EMAILS:
                out["skipped"] = f"over the {MAX_FOLLOWUP_EMAILS}-email cap for one run"
                emails.append(out)
                continue
            out["body"] = followup_body(items)
            if dry_run:
                out["would_send"] = True
                emails.append(out)
                continue
            try:
                draft_id = _send_reply(FOLLOWUP_FROM, th["reply_to_graph_id"],
                                       FOLLOWUP_TO, FOLLOWUP_CC, out["subject"],
                                       out["body"])
            except Exception as exc:  # noqa: BLE001
                out["error"] = _safe_err(exc, "graph send")
                emails.append(out)
                continue
            out["sent"] = True
            sent_any = True
            for i in items:
                tracking[i["key"]] = record_chase(tracking[i["key"]], i["ask"], now,
                                                  draft_id)
            emails.append(out)
        if sent_any:
            save_po_delivery(tracking)
        return jsonify({"ok": True, "dry_run": dry_run,
                        "synced_at": cache.get("synced_at"),
                        "emails": emails,
                        "sent": sum(1 for e in emails if e.get("sent")),
                        "due_without_thread": no_thread})
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "delivery-send-followups")
        return jsonify({"ok": False, "error": _safe_err(exc), "emails": []}), 200
