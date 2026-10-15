"""Lineage delivery tracking routes -- scheduled ETA, changes, confirmed arrival.

The free-text reading of Lineage's replies happens in the "Lineage PO delivery
check" scheduled task; these routes are what it talks to:

  POST /api/pos/delivery/sync-threads  fetch the "Quote and Schedule" threads
                                       (server-side Graph), register each open
                                       PO they list, return the threads + the
                                       messages to read
  POST /api/pos/delivery/events        apply what the task read (scheduled /
                                       rescheduled / delivered), each with its
                                       evidence; moves the PO's dates and, on
                                       delivery, releases it into on-hand
  POST /api/pos/delivery/chased        record the follow-ups it sent
  GET  /api/pos/delivery               tracked POs + what follow-up is due

Rules live in integrations/po_delivery.py; the rollover hold in
inventory_tracker._delivery_gate.
"""
import os
import urllib.parse
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from core.errors import _log_exc, _safe_err
from core.util import with_data_lock

delivery_bp = Blueprint("delivery", __name__)

THREAD_SUBJECT = "Quote and Schedule"


def _now():
    return datetime.now(timezone.utc)


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


def _fetch_thread_messages(lookback_days: int) -> tuple:
    """Every "Quote and Schedule" message in the mailboxes, deduped across
    them (preferring JD's copy, whose Graph id a threaded reply needs)."""
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
    search = urllib.parse.quote(f'"subject:{THREAD_SUBJECT}"')
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
                if THREAD_SUBJECT.lower() not in (m.get("subject") or "").lower():
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
    from inventory_tracker import load_po_delivery, save_po_delivery
    from integrations.po_delivery import (
        apply_event, is_hh, norm_po, parse_request_blocks, summary,
    )
    try:
        body = request.get_json(silent=True) or {}
        lookback = max(3, min(int(body.get("lookback_days") or 45), 120))
        dry_run = bool(body.get("dry_run", False))
        msgs, errors = _fetch_thread_messages(lookback)
        convs: dict = {}
        for m in msgs:
            convs.setdefault(m["conversation_id"], []).append(m)
        tracking = load_po_delivery()
        ledger = _ledger_by_po()
        now = _now()
        threads, registered, not_in_tracker = [], [], []
        for conv, ms in convs.items():
            ms.sort(key=lambda m: m["sent"])
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
        if registered and not dry_run:
            save_po_delivery(tracking)
        threads.sort(key=lambda t: t["messages"][-1]["sent"], reverse=True)
        return jsonify({"ok": True, "lookback_days": lookback, "dry_run": dry_run,
                        "threads": threads, "registered": registered,
                        "not_in_tracker": sorted(set(not_in_tracker)),
                        "errors": errors})
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "delivery-sync-threads")
        return jsonify({"ok": False, "error": _safe_err(exc), "threads": []}), 200


@delivery_bp.route("/api/pos/delivery/events", methods=["POST"])
@with_data_lock
def api_delivery_events():
    """Apply events read off the threads. Body {events:[{po_number, event,
    delivery_appt?, pickup_at?, delivered_at?, confirmation_no?, evidence:
    {message_id, sender, sent, quote}}], dry_run}. Dates are ISO with the
    receiving dock's UTC offset. Each result says what moved, or why not."""
    from inventory_tracker import load_po_delivery, save_po_delivery
    from integrations.po_delivery import apply_event, norm_po, summary
    try:
        body = request.get_json(silent=True) or {}
        dry_run = bool(body.get("dry_run", False))
        events = body.get("events") or []
        if not isinstance(events, list):
            return jsonify({"ok": False, "error": "events must be a list"}), 400
        tracking = load_po_delivery()
        ledger = _ledger_by_po()
        now = _now()
        results, touched = [], []
        for ev in events:
            ev = ev if isinstance(ev, dict) else {}
            key = norm_po(ev.get("po_number") or "")
            lr = ledger.get(key)
            if lr is None and key not in tracking:
                results.append({"po_number": ev.get("po_number"), "applied": False,
                                "reason": "PO not in the tracker"})
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
        if touched and not dry_run:
            save_po_delivery(tracking)
            for key in dict.fromkeys(touched):
                inventory[key] = _apply_to_inventory(key, tracking[key])
            after = _ledger_by_po()
            for r in results:
                if r.get("applied"):
                    k = norm_po(r["po_number"])
                    r["inventory"] = inventory.get(k, {})
                    r["ledger_status_after"] = (after.get(k) or {}).get("status", "")
        return jsonify({"ok": True, "dry_run": dry_run, "results": results})
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
