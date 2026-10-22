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


def _closed_reason(ledger_rec) -> str:
    """Why a tracked PO is no longer worth following up, or ''. Canceling a
    PO removes its pending rows, so a canceled USF/Cheney PO drops out of the
    ledger altogether; a canceled CW PO stays in it as "canceled"."""
    if ledger_rec is None:
        return "no longer in the PO ledger (canceled or removed)"
    if (ledger_rec.get("status") or "") in ("canceled", "cancelled"):
        return "canceled in the tracker"
    return ""


def _is_open(ledger_rec, trk) -> bool:
    """Still worth following up: on order (not arrived / canceled), or tracked
    and not yet confirmed delivered -- but never a PO the ledger says is
    canceled or no longer has. Until 10/22 a tracked "requested" PO stayed
    open forever, so a canceled PO kept getting "could you send the pickup
    date" emails to Lineage every 44 h."""
    from integrations.po_delivery import HOLD_STATUSES
    if _closed_reason(ledger_rec):
        return False
    if trk and trk.get("status") in HOLD_STATUSES:
        return True
    return (ledger_rec.get("status") or "") not in ("arrived", "canceled", "cancelled")


def _stamp_closed(tracking: dict, ledger: dict, now) -> list:
    """Stamp closed_reason on held records whose PO the ledger dropped or
    canceled (cleared again if it comes back, e.g. /api/admin/uncancel-po).
    Mutates ``tracking``; returns the keys that changed."""
    from integrations.po_delivery import HOLD_STATUSES
    changed = []
    for key, rec in list(tracking.items()):
        why = (_closed_reason(ledger.get(key))
               if rec.get("status") in HOLD_STATUSES else "")
        if why and rec.get("closed_reason") != why:
            tracking[key] = {**rec, "closed_reason": why, "closed_at": now.isoformat()}
            changed.append(key)
        elif not why and rec.get("closed_reason"):
            tracking[key] = {k: v for k, v in rec.items()
                             if k not in ("closed_reason", "closed_at")}
            changed.append(key)
    return changed


def _apply_to_inventory(po_key: str, rec: dict) -> dict:
    """Mirror a tracking change onto the PO's pending rows (dates shown on the
    Pending tab). The hold / release itself is decided by the rollover gate,
    which reads the tracking file -- load_inventory() below already applies it,
    so a confirmed delivery rolls into on-hand right here."""
    from inventory_tracker import load_inventory, save_inventory
    from integrations.po_delivery import delivered_local, norm_po, wall_time
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
                local = delivered_local(rec)      # date only when no time given
                e["arrival_date"] = local
                e["eta"] = local
                e["arrival_source"] = "lineage-delivered"
            if (rec.get("pickup_at") and st != "delivered"
                    and e.get("ship_date_source") != "freight"):
                e["ship_date"] = wall_time(rec["pickup_at"])
                e["ship_date_source"] = "lineage"
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
def api_delivery_sync_threads():
    """Fetch the Quote and Schedule threads, register each open PO they list
    (status "requested"), and return the threads that still have an open PO,
    with their messages oldest-first and the id to reply on."""
    from inventory_tracker import (
        data_lock, load_po_delivery, load_po_delivery_threads, save_po_delivery,
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
        # The data lock only from here. The Graph reads above can take tens of
        # seconds and every load_inventory() -- every page of the app -- waits
        # on this lock (review 2026-10-22). Tracking is read inside it, so a
        # concurrent events post is never overwritten.
        with data_lock():
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
            closed = _stamp_closed(tracking, ledger, now)
            if (registered or closed) and not dry_run:
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
                            "closed": [tracking[k].get("po_number") or k for k in closed
                                       if tracking[k].get("closed_reason")],
                            "not_in_tracker": sorted(set(not_in_tracker)),
                            "evidence_threads": evidence_threads,
                            "evidence_errors": ev_errors,
                            "errors": errors})
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "delivery-sync-threads")
        return jsonify({"ok": False, "error": _safe_err(exc), "threads": []}), 200


def _answers_only(msg: dict, po_key: str, pos_on_thread: list, cache: dict) -> bool:
    """Is ``msg`` a reply to an H&H message that asked about ``po_key`` and no
    other PO on the thread? (JD's follow-up "- PO 1209157 (...): can you
    confirm it delivered" -> Lineage: "This shows delivered.")"""
    from integrations.po_delivery import (INTERNAL_TEST_PREFIX, is_hh, names_po,
                                          own_text, parse_dt)
    sent = parse_dt(msg.get("sent") or "")
    conv = msg.get("conversation_id") or ""
    prev = None
    for m in (cache.get("messages") or {}).values():
        if m.get("conversation_id") != conv or not is_hh(m.get("from") or ""):
            continue
        if (m.get("text") or "").lstrip().startswith(INTERNAL_TEST_PREFIX):
            continue
        t = parse_dt(m.get("sent") or "")
        if t is None or sent is None or t >= sent:
            continue
        if prev is None or t > prev[0]:
            prev = (t, m)
    if prev is None:
        return False
    text = own_text(prev[1].get("text") or "")
    return [p for p in dict.fromkeys(pos_on_thread) if names_po(text, p)] == [po_key]


def _verify_evidence(ev: dict, po_key: str, cache: dict, tracked: bool = True) -> tuple:
    """Check an event's evidence against what the last sync read from the
    mailbox. Returns (event, None) with sender / sent replaced by the
    mailbox's own values, or (event, reason) when it can't be trusted: the
    message wasn't read, its thread doesn't list this PO, the quote isn't in
    it, or -- on a thread about several POs -- nothing ties the message to
    THIS PO. (The sender rule -- Lineage or the DC only -- is apply_event's.)"""
    from integrations.po_delivery import names_po, own_text, quote_in_text
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
    # A thread about several POs: a line proves something about THIS PO only
    # if the message names it in its own text, or answers an H&H message that
    # asked about it alone. Otherwise "Yes this shows delivered" on the
    # 393072B2 / 014511763291 thread could release either (review 10/22).
    pos_on_thread = list(dict.fromkeys(th.get("po_numbers") or []))
    if (len(pos_on_thread) > 1
            and not names_po(own_text(msg.get("text") or ""), po_key)
            and not _answers_only(msg, po_key, pos_on_thread, cache)):
        return ev, (f"that thread covers {len(pos_on_thread)} POs and the message "
                    f"doesn't say which -- quote a message that names {po_key}, or "
                    f"one answering an H&H message that asked about {po_key} alone")
    # A schedule read off a one-off delivery thread can't START tracking a PO:
    # tracking holds the PO out of on-hand, and nothing can chase a PO that
    # isn't on a Quote and Schedule thread (it would sit held, unchased).
    kind = str(ev.get("event") or "").strip().lower()
    if th.get("evidence_only") and not tracked and kind in ("scheduled", "rescheduled"):
        return ev, ("this PO isn't tracked, and a schedule from a one-off delivery "
                    "thread can't start tracking it (only the Quote and Schedule "
                    "request does)")
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
                if kind not in ("scheduled", "rescheduled", "picked_up", "delivered"):
                    results.append({"po_number": ev.get("po_number"), "applied": False,
                                    "reason": f"'{kind}' isn't allowed with the delivery key"})
                    continue
            if kind in ("scheduled", "rescheduled", "picked_up", "delivered"):
                ev, why = _verify_evidence(ev, key, cache, tracked=key in tracking)
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


def _graph_call(sender: str):
    """(call, box) for Graph requests from ``sender``'s mailbox -- the same
    calls /api/email/send makes."""
    import urllib.request
    from core.http import _TRUSTED_OUTBOUND_HOSTS
    from integrations.email_scanner import GRAPH_BASE, EmailInboxClient
    token = EmailInboxClient()._ms365_token()

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

    return call, f"{GRAPH_BASE}/users/{urllib.parse.quote(sender)}/messages"


def _create_reply(sender: str, reply_to_graph_id: str) -> str:
    """createReply on the thread's latest message; returns the draft id."""
    call, box = _graph_call(sender)
    uq = urllib.parse.quote
    draft = json.loads(call("POST", f"{box}/{uq(reply_to_graph_id)}/createReply") or "{}")
    draft_id = draft.get("id")
    if not draft_id:
        raise RuntimeError("createReply returned no draft id")
    return draft_id


def _send_draft(sender: str, draft_id: str, to, cc, subject: str, body_text: str) -> None:
    """Fill the reply draft (fixed recipients / wording) and send it."""
    call, box = _graph_call(sender)
    uq = urllib.parse.quote
    rc = lambda addrs: [{"emailAddress": {"address": a}} for a in addrs]  # noqa: E731
    call("PATCH", f"{box}/{uq(draft_id)}", {
        "subject": subject, "body": {"contentType": "Text", "content": body_text},
        "toRecipients": rc(to), "ccRecipients": rc(cc)})
    call("POST", f"{box}/{uq(draft_id)}/send")


def _set_chase_state(keys, claim: str, message_id: str = None, send_state: str = None,
                     drop: bool = False) -> None:
    """Under the data lock: re-read tracking and update (or drop) the chases
    this run recorded with message_id ``claim`` on ``keys``."""
    from inventory_tracker import data_lock, load_po_delivery, save_po_delivery
    with data_lock():
        tracking = load_po_delivery()
        for key in keys:
            rec = tracking.get(key)
            if not rec:
                continue
            chases = []
            for c in rec.get("chases") or []:
                if c.get("message_id") == claim:
                    if drop:
                        continue
                    c = dict(c)
                    if message_id is not None:
                        c["message_id"] = message_id
                    if send_state is not None:
                        c["send_state"] = send_state
                chases.append(c)
            tracking[key] = {**rec, "chases": chases}
        save_po_delivery(tracking)


@delivery_bp.route("/api/pos/delivery/send-followups", methods=["POST"])
def api_delivery_send_followups():
    """Write and send the follow-ups that are due, one threaded email per
    Quote and Schedule thread, then record them. Body {dry_run}.

    Everything about the email is decided here, not by the caller: who is due
    (po_delivery.chase_due), the recipients (FOLLOWUP_TO / FOLLOWUP_CC), the
    wording (ASK_TEXT), and the thread (the NEWEST Quote and Schedule thread
    listing the PO, from the last sync). Guards: the sync must be fresh
    (SYNC_FRESH_FOR) and complete; a PO someone wrote about on ANY cached
    thread in the last THREAD_QUIET_FOR is held back (po_delivery.quiet_pos);
    a canceled PO, or one the ledger no longer has, is never chased; at most
    MAX_FOLLOWUP_EMAILS.

    Graph calls run OUTSIDE the data lock (every page waits on it). Each email
    is claimed under the lock first -- its POs re-checked as still due and a
    chase recorded -- so two overlapping runs can't both send it; the chase is
    then stamped "draft" once createReply succeeds and "sent" after the send.
    A send that fails AFTER the draft exists still counts as chased (no resend
    within 16 h: Graph may have sent it) and is reported."""
    import uuid
    from inventory_tracker import (
        data_lock, load_po_delivery, load_po_delivery_threads, save_po_delivery,
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
        with data_lock():
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
            closed = _stamp_closed(tracking, ledger, now)
            if closed and not dry_run:
                save_po_delivery(tracking)

        # "Someone just wrote about it", per PO, across EVERY cached thread that
        # names it -- a Lineage answer on a newer thread, or on a one-off
        # "PO ... Delivery Status" thread, holds the chase too.
        by_conv: dict = {}
        for m in (cache.get("messages") or {}).values():
            by_conv.setdefault(m.get("conversation_id") or "", []).append(m)
        quiet_by_conv = {c: quiet_pos(by_conv.get(c, []), t.get("po_numbers") or [], now)
                         for c, t in threads.items()}

        groups, no_thread, held_by_conv = {}, [], {}
        for key, rec in tracking.items():
            lr = ledger.get(key)
            if not _is_open(lr, rec):
                continue
            ask = chase_due(rec, now)
            if not ask:
                continue
            # The NEWEST Quote and Schedule thread listing the PO -- not the one
            # it was first registered from: when Isaiah re-requests a PO on a
            # new thread, that is where Lineage is answering.
            conv = max((c for c, t in threads.items()
                        if key in (t.get("po_numbers") or []) and not t.get("evidence_only")),
                       key=lambda c: threads[c].get("last_sent") or "", default="")
            if not conv:
                no_thread.append(rec.get("po_number") or key)
                continue
            item = {"key": key, "po_number": rec.get("po_number") or key, "ask": ask,
                    "distributor": (lr or {}).get("distributor", ""),
                    "warehouse": (lr or {}).get("warehouse", ""),
                    "delivery_appt": rec.get("delivery_appt") or ""}
            why = next((q[key] for q in quiet_by_conv.values() if key in q), None)
            if why:
                held_by_conv.setdefault(conv, {})[item["po_number"]] = why
                continue
            groups.setdefault(conv, []).append(item)

        emails = []
        for conv in sorted(set(groups) | set(held_by_conv),
                           key=lambda c: threads[c].get("last_sent") or ""):
            th = threads[conv]
            items = sorted(groups.get(conv, []), key=lambda i: i["po_number"])
            held = held_by_conv.get(conv, {})
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
            if len([e for e in emails if e.get("sent") or e.get("would_send")
                    or e.get("send_state")]) >= MAX_FOLLOWUP_EMAILS:
                out["skipped"] = f"over the {MAX_FOLLOWUP_EMAILS}-email cap for one run"
                emails.append(out)
                continue
            out["body"] = followup_body(items)
            if dry_run:
                out["would_send"] = True
                emails.append(out)
                continue

            # 1) Claim: still due (an event or another run may have landed
            #    since the read above)? Record the chase before any Graph call.
            claim = f"claim:{uuid.uuid4().hex}"
            with data_lock():
                tracking = load_po_delivery()
                still = [i for i in items if i["key"] in tracking
                         and chase_due(tracking[i["key"]], now) == i["ask"]]
                for i in still:
                    tracking[i["key"]] = record_chase(tracking[i["key"]], i["ask"], now,
                                                      claim, send_state="claimed")
                if still:
                    save_po_delivery(tracking)
            if len(still) != len(items):
                items = still
                out["po_numbers"] = [i["po_number"] for i in items]
                out["asks"] = {i["po_number"]: i["ask"] for i in items}
                if not items:
                    out["skipped"] = "no longer due (chased or updated meanwhile)"
                    out.pop("body", None)
                    emails.append(out)
                    continue
                out["body"] = followup_body(items)
            keys = [i["key"] for i in items]
            # 2) createReply. Nothing exists yet if it fails: drop the claim.
            try:
                draft_id = _create_reply(FOLLOWUP_FROM, th["reply_to_graph_id"])
            except Exception as exc:  # noqa: BLE001
                _set_chase_state(keys, claim, drop=True)
                out["error"] = _safe_err(exc, "graph createReply")
                emails.append(out)
                continue
            _set_chase_state(keys, claim, message_id=draft_id, send_state="draft")
            out["draft_id"] = draft_id
            out["send_state"] = "draft"
            # 3) Fill + send. A failure here may still have sent (a timeout on
            #    the reply): the chase stays recorded as "draft" -- no resend
            #    within 16 h -- and the error says to check JD's Drafts.
            try:
                _send_draft(FOLLOWUP_FROM, draft_id, FOLLOWUP_TO, FOLLOWUP_CC,
                            out["subject"], out["body"])
            except Exception as exc:  # noqa: BLE001
                out["error"] = (_safe_err(exc, "graph send") + " -- the reply draft "
                                "exists, so this counts as chased (no resend for 16 h); "
                                "check JD's Drafts / Sent Items")
                emails.append(out)
                continue
            _set_chase_state(keys, draft_id, send_state="sent")
            out["send_state"] = "sent"
            out["sent"] = True
            emails.append(out)
        return jsonify({"ok": True, "dry_run": dry_run,
                        "synced_at": cache.get("synced_at"),
                        "emails": emails,
                        "sent": sum(1 for e in emails if e.get("sent")),
                        "unconfirmed_sends": [e["conversation_id"] for e in emails
                                              if e.get("send_state") == "draft"],
                        "closed": [tracking[k].get("po_number") or k for k in closed
                                   if (tracking.get(k) or {}).get("closed_reason")],
                        "due_without_thread": no_thread})
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "delivery-send-followups")
        return jsonify({"ok": False, "error": _safe_err(exc), "emails": []}), 200
