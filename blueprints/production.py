"""Production blueprint — production-run records: list, lots-by-pair FIFO,
summary, ingest, the MS365 production-PDF scan, variety renormalize /
reclassify admin ops, and delete. Extracted from app.py (refactor — see
REFACTOR_PLAN.md). Shared helpers come from core/."""
import os
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from core.cache import _AGG_CACHE, _data_sig
from core.errors import _safe_err
from inventory_tracker import load_inventory, load_usage

production_bp = Blueprint("production", __name__)


@production_bp.route("/api/production")
def api_production_list():
    """List production records. Optional query params: distributor,
    warehouse, since (ISO date). Returns newest-first."""
    from inventory_tracker import load_production
    records = load_production()
    dist = (request.args.get("distributor") or "").strip()
    wh   = (request.args.get("warehouse") or "").strip()
    since = (request.args.get("since") or "").strip()
    out = []
    for r in records:
        if dist and (r.get("distributor") or "") != dist:
            continue
        if wh and (r.get("warehouse") or "") != wh:
            continue
        if since and (r.get("production_date") or "") < since:
            continue
        out.append(r)
    out.sort(key=lambda r: (r.get("production_date") or "", r.get("po_number") or ""),
             reverse=True)
    return jsonify(out)


@production_bp.route("/api/production/lots-by-pair")
def api_production_lots_by_pair():
    """Lot-level breakdown grouped by (warehouse, variety) with FIFO state.

    Returns a dict keyed by ``"<warehouse>|<variety>"`` whose values are
    **oldest-first** lists of:

        {lot, cs, cs_produced, cs_consumed, cs_remaining,
         is_active, is_next_out,
         production_date, po_number, received_at}

    The FIFO rule (consume earliest lot first) is computed centrally in
    ``compute_lot_fifo_state``; this endpoint just enumerates the pairs and
    delegates so the front-end and any future server-side consumers see the
    same numbers. ``cs`` is preserved as an alias for ``cs_produced`` for
    backward compatibility with older JS revisions.
    """
    sig = _data_sig("production.json", "usage.json", "inventory.json")
    _c = _AGG_CACHE.get("lots-by-pair")
    if _c and _c[0] == sig:
        return jsonify(_c[1])
    from inventory_tracker import (
        load_production, load_usage, load_inventory, compute_lot_fifo_state,
    )
    records = load_production()
    usage = load_usage()
    inv_snapshot = load_inventory()

    # Discover all distinct (warehouse, variety) pairs we have lots for.
    pairs: set = set()
    for r in records:
        wh = r.get("warehouse") or ""
        if not wh:
            continue
        for L in r.get("lines", []):
            lot = (L.get("lot_number") or "").strip()
            if not lot:
                continue
            variety = L.get("variety") or ""
            if not variety:
                continue
            pairs.add((wh, variety))

    out: dict = {}
    for (wh, variety) in pairs:
        lots = compute_lot_fifo_state(
            wh, variety,
            production_records=records,
            usage_records=usage,
            inventory_snapshot=inv_snapshot,
        )
        # Surface cs as an alias for cs_produced so any older client that
        # still reads L.cs keeps working until it is redeployed.
        for L in lots:
            L["cs"] = L["cs_produced"]
        out[f"{wh}|{variety}"] = lots
    _AGG_CACHE["lots-by-pair"] = (sig, out)
    return jsonify(out)


@production_bp.route("/api/production/summary")
def api_production_summary():
    """Roll-up across production records.

    Query params:
      period  = "day" | "week" | "month"  (default "week")

    Returns:
      {
        "period": "week",
        "buckets": [
          { "key": "2026-W19", "start": "2026-05-04", "end": "2026-05-10",
            "total_cs": 1232,
            "by_distributor": {"Cheney Brothers": 224, "US Foods": 1008, ...},
            "by_variety": {"Plain": 176, "Everything": 184, ...}
          }, ...
        ]
      }
    """
    from datetime import datetime, timedelta
    from inventory_tracker import load_production
    records = load_production()
    period = (request.args.get("period") or "week").lower()

    def bucket_key(iso_date: str) -> tuple:
        try:
            dt = datetime.fromisoformat(iso_date)
        except (TypeError, ValueError):
            return ("", "", "")
        if period == "day":
            return (iso_date, iso_date, iso_date)
        if period == "month":
            start = dt.replace(day=1)
            # Last day of month: jump to next month then back one day
            if start.month == 12:
                nxt = start.replace(year=start.year + 1, month=1)
            else:
                nxt = start.replace(month=start.month + 1)
            end = nxt - timedelta(days=1)
            return (f"{start.year:04d}-{start.month:02d}",
                    start.date().isoformat(), end.date().isoformat())
        # default "week" — ISO weeks (Monday-start)
        iso = dt.isocalendar()
        start = dt - timedelta(days=dt.weekday())
        end = start + timedelta(days=6)
        return (f"{iso.year:04d}-W{iso.week:02d}",
                start.date().isoformat(), end.date().isoformat())

    buckets: dict = {}
    for r in records:
        key, start, end = bucket_key(r.get("production_date") or "")
        if not key:
            continue
        b = buckets.setdefault(key, {
            "key": key, "start": start, "end": end,
            "total_cs": 0, "by_distributor": {}, "by_variety": {},
        })
        b["total_cs"] += int(r.get("total_cases") or 0)
        d = r.get("distributor") or "Unassigned"
        b["by_distributor"][d] = b["by_distributor"].get(d, 0) + int(r.get("total_cases") or 0)
        for line in (r.get("lines") or []):
            v = line.get("variety") or "Unknown"
            b["by_variety"][v] = b["by_variety"].get(v, 0) + int(line.get("cs_count") or 0)
    return jsonify({
        "period": period,
        "buckets": sorted(buckets.values(), key=lambda x: x["start"], reverse=True),
    })


@production_bp.route("/api/production/ingest", methods=["POST"])
def api_production_ingest():
    """Parse a Daily Production Sheet PDF and store as a record.

    Body:
      pdf_b64       base64-encoded PDF bytes (required)
      subject       email subject (optional, used as a fallback PO source)
      sender        email From address (optional, kept for audit)
      message_id    Graph internetMessageId or RFC822 Message-Id (required
                    for idempotency — re-posting the same message is a no-op)
      received_at   ISO timestamp (optional)

    Returns 200 with {ok, status: "ingested"|"duplicate"|"parse_error",
    record, error}. parse_error is surfaced for image-only scan PDFs so
    the operator can re-request a text PDF.
    """
    import base64
    from datetime import datetime
    from inventory_tracker import load_production, save_production
    from integrations.production_pdf_parser import parse_production_pdf

    body = request.json or {}
    pdf_b64 = body.get("pdf_b64") or ""
    if not pdf_b64:
        return jsonify({"ok": False, "error": "pdf_b64 required"}), 400
    subject    = (body.get("subject") or "").strip()
    sender     = (body.get("sender") or "").strip()
    message_id = (body.get("message_id") or "").strip()
    received_at = (body.get("received_at") or "").strip()

    records = load_production()
    if message_id:
        for existing in records:
            if existing.get("source_message_id") == message_id:
                return jsonify({
                    "ok": True,
                    "status": "duplicate",
                    "record": existing,
                })

    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc, "pdf_b64")}), 400

    sheet = parse_production_pdf(pdf_bytes, subject=subject)

    # Content-signature dedup. Same email can land twice with two different
    # IDs (RFC <...@outlook.com> Message-ID vs. Microsoft Graph
    # internetMessageId) when more than one scanner path picks it up — see
    # the 2026-05-19 incident where five Daily Production records got stored
    # twice. The message_id check above only catches *exact* re-sends; this
    # block catches the cross-ID dup by matching on (warehouse, po_number,
    # production_date) + identical line signature. Legitimate amendments
    # (different lines/cs counts) still pass through.
    if (sheet.po_number and sheet.warehouse and sheet.production_date
            and sheet.lines):
        incoming_sig = tuple(sorted(
            (L.variety or "", int(L.cs_count or 0), L.lot_number or "")
            for L in sheet.lines))
        for existing in records:
            if (existing.get("warehouse") != sheet.warehouse
                    or existing.get("po_number") != sheet.po_number
                    or existing.get("production_date") != sheet.production_date):
                continue
            existing_sig = tuple(sorted(
                (L.get("variety") or "", int(L.get("cs_count") or 0),
                 L.get("lot_number") or "")
                for L in (existing.get("lines") or [])))
            if existing_sig == incoming_sig:
                return jsonify({
                    "ok": True,
                    "status": "duplicate",
                    "record": existing,
                    "dedup_reason": "content_signature_match",
                })

    if sheet.error and not sheet.lines:
        # Persist a stub for the dashboard so the operator can see the
        # email arrived but needs manual entry, then surface the error.
        stub = {
            "production_date": "",
            "warehouse": "",
            "warehouse_raw": "",
            "distributor": "",
            "po_number": "",
            "lines": [],
            "total_cases": 0,
            "unmapped_varieties": [],
            "source_message_id": message_id,
            "source_subject": subject,
            "source_sender": sender,
            "received_at": received_at or datetime.now().isoformat(),
            "ingested_at": datetime.now().isoformat(),
            "parse_error": sheet.error,
        }
        records.append(stub)
        save_production(records)
        return jsonify({
            "ok": True,
            "status": "parse_error",
            "record": stub,
            "error": sheet.error,
        })

    record = {
        "production_date":    sheet.production_date,
        "warehouse":          sheet.warehouse,
        "warehouse_raw":      sheet.warehouse_raw,
        "distributor":        sheet.distributor,
        "po_number":          sheet.po_number,
        "lines": [
            {"variety": L.variety, "raw_variety": L.raw_variety,
             "cs_count": L.cs_count, "lot_number": L.lot_number}
            for L in sheet.lines
        ],
        "total_cases":        sheet.total_cases,
        "unmapped_varieties": sheet.unmapped_varieties,
        "source_message_id":  message_id,
        "source_subject":     subject,
        "source_sender":      sender,
        "received_at":        received_at or datetime.now().isoformat(),
        "ingested_at":        datetime.now().isoformat(),
        "parse_error":        "",
    }
    records.append(record)
    save_production(records)
    return jsonify({"ok": True, "status": "ingested", "record": record})


@production_bp.route("/api/production/scan", methods=["POST"])
def api_production_scan():
    """Wide-lookback scan of mailbox(es) for Daily Production sheet emails.

    Mirrors the existing /api/email/scan flow but routes recognized
    Daily Production emails (sender = *@hhbagels.com, subject contains
    "Daily Production") through the production PDF parser into
    data/production.json instead of the on_order pipeline.

    Body:
      lookback_days  int   how far back to look (defaults to 365)
      max_messages   int   per-mailbox cap on qualified messages
                           (defaults to 200, hard cap 2000)
      dry_run        bool  parse but don't persist

    Returns {ok, scanned, ingested, parse_errors, records:[brief]}.
    """
    import base64
    import traceback as _tb
    from datetime import datetime, timezone, timedelta
    try:
        from integrations.email_scanner import (
            EmailInboxClient, _distributor_from_sender, GRAPH_BASE,
        )
        from integrations.production_pdf_parser import parse_production_pdf
        from inventory_tracker import load_production, save_production
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc, "import"),
                        "traceback": ""}), 500

    body = request.json or {}
    try:
        lookback_days = int(body.get("lookback_days") or 365)
    except (TypeError, ValueError):
        lookback_days = 365
    try:
        until_days = int(body.get("until_days") or 0)
    except (TypeError, ValueError):
        until_days = 0
    try:
        max_messages = int(body.get("max_messages") or 200)
    except (TypeError, ValueError):
        max_messages = 200
    max_messages = max(1, min(max_messages, 2000))
    dry_run = bool(body.get("dry_run", False))
    refresh = bool(body.get("refresh", False))

    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    since_iso = since.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    until_iso = None
    if until_days > 0:
        until = datetime.now(timezone.utc) - timedelta(days=until_days)
        until_iso = until.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    client = EmailInboxClient()
    try:
        token = client._ms365_token()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc, "ms365 token")}), 500

    import os, json as _json, urllib.parse, urllib.request, urllib.error
    users = [u.strip() for u in os.environ.get("MS365_USER", "").split(",") if u.strip()]
    folder = urllib.parse.quote(os.environ.get("MS365_FOLDER", "Inbox"))

    scanned = 0
    qualifying_count = 0
    ingested = 0
    parse_errors = []
    brief_records = []
    existing = load_production()
    seen_msg_ids = {r.get("source_message_id") for r in existing if r.get("source_message_id")}
    touched_msg_ids = set()  # records modified or added in THIS run

    for upn in users:
        user = urllib.parse.quote(upn)
        # Build a date filter; narrow to a window if `until_days` was given.
        date_filter = f"receivedDateTime ge {since_iso}"
        if until_iso:
            date_filter += f" and receivedDateTime lt {until_iso}"
        qs = urllib.parse.urlencode({
            "$top": "100",
            "$select": ("id,subject,from,toRecipients,receivedDateTime,"
                        "hasAttachments,internetMessageId"),
            "$filter": f"hasAttachments eq true and {date_filter}",
        })
        next_url = f"{GRAPH_BASE}/users/{user}/mailFolders/{folder}/messages?{qs}"
        pages = 0
        per_mailbox = 0
        while next_url and pages < 40 and per_mailbox < max_messages:
            pages += 1
            try:
                req = urllib.request.Request(next_url, method="GET")
                req.add_header("Authorization", f"Bearer {token}")
                req.add_header("Accept", "application/json")
                req.add_header("ConsistencyLevel", "eventual")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    page = _json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                parse_errors.append(f"{upn} page {pages}: parse error")
                break
            for m in page.get("value", []):
                scanned += 1
                if per_mailbox >= max_messages:
                    break
                subject = m.get("subject") or ""
                sender = (((m.get("from") or {}).get("emailAddress") or {})
                          .get("address") or "")
                # Production emails: sender on hhbagels.com + subject says
                # "Daily Production" (case-flex)
                dom = sender.split("@")[-1].lower() if "@" in sender else ""
                if not (dom == "hhbagels.com" or dom.endswith(".hhbagels.com")):
                    continue
                if "daily production" not in subject.lower():
                    continue
                qualifying_count += 1
                msg_id = m.get("internetMessageId") or m.get("id") or ""
                if msg_id and msg_id in seen_msg_ids and not refresh:
                    continue  # already ingested
                # Pull the attachments list
                att_url = f"{GRAPH_BASE}/users/{user}/messages/{m.get('id')}/attachments"
                try:
                    araw, _ = client._graph_get(att_url, token)
                    apage = _json.loads(araw.decode("utf-8"))
                except Exception as exc:  # noqa: BLE001
                    parse_errors.append(f"{subject[:60]!r}: list-att failed")
                    continue
                pdf_atts = [a for a in apage.get("value", [])
                            if (a.get("name") or "").lower().endswith(".pdf")
                            or (a.get("contentType") or "").lower() == "application/pdf"]
                if not pdf_atts:
                    continue
                # Fetch + parse the first PDF attachment
                a = pdf_atts[0]
                acid = a.get("id")
                fetch_url = f"{GRAPH_BASE}/users/{user}/messages/{m.get('id')}/attachments/{acid}"
                try:
                    fraw, _ = client._graph_get(fetch_url, token)
                    apayload = _json.loads(fraw.decode("utf-8"))
                    pdf_bytes = base64.b64decode(apayload.get("contentBytes") or "")
                except Exception as exc:  # noqa: BLE001
                    parse_errors.append(f"{subject[:60]!r}: fetch-att failed")
                    continue
                sheet = parse_production_pdf(pdf_bytes, subject=subject)
                if sheet.error and not sheet.lines:
                    parse_errors.append(f"{subject[:60]!r}: {sheet.error}")
                    if dry_run:
                        per_mailbox += 1
                        continue
                    # Persist a stub so the operator sees it in the UI
                    record = {
                        "production_date": "", "warehouse": "", "warehouse_raw": "",
                        "distributor": "", "po_number": "", "lines": [],
                        "total_cases": 0, "unmapped_varieties": [],
                        "source_message_id": msg_id,
                        "source_subject": subject, "source_sender": sender,
                        "received_at": m.get("receivedDateTime") or "",
                        "ingested_at": datetime.now().isoformat(),
                        "parse_error": sheet.error,
                    }
                    existing.append(record)
                    seen_msg_ids.add(msg_id)
                    if msg_id:
                        touched_msg_ids.add(msg_id)
                    brief_records.append({"subject": subject, "po_number": "",
                                          "warehouse": "", "total_cases": 0,
                                          "parse_error": sheet.error})
                    per_mailbox += 1
                    continue
                if dry_run:
                    per_mailbox += 1
                    continue
                record = {
                    "production_date":    sheet.production_date,
                    "warehouse":          sheet.warehouse,
                    "warehouse_raw":      sheet.warehouse_raw,
                    "distributor":        sheet.distributor,
                    "po_number":          sheet.po_number,
                    "lines": [
                        {"variety": L.variety, "raw_variety": L.raw_variety,
                         "cs_count": L.cs_count, "lot_number": L.lot_number}
                        for L in sheet.lines
                    ],
                    "total_cases":        sheet.total_cases,
                    "unmapped_varieties": sheet.unmapped_varieties,
                    "source_message_id":  msg_id,
                    "source_subject":     subject,
                    "source_sender":      sender,
                    "received_at":        m.get("receivedDateTime") or "",
                    "ingested_at":        datetime.now().isoformat(),
                    "parse_error":        "",
                }
                if refresh and msg_id and msg_id in seen_msg_ids:
                    # Update in place rather than append a duplicate.
                    for i, r in enumerate(existing):
                        if r.get("source_message_id") == msg_id:
                            # Preserve original received_at if already set;
                            # overwrite parsed-from-PDF fields with the
                            # fresh values.
                            record["received_at"] = r.get("received_at") or record["received_at"]
                            existing[i] = record
                            break
                else:
                    existing.append(record)
                    seen_msg_ids.add(msg_id)
                if msg_id:
                    touched_msg_ids.add(msg_id)
                ingested += 1
                brief_records.append({
                    "subject": subject, "po_number": sheet.po_number,
                    "warehouse": sheet.warehouse, "production_date": sheet.production_date,
                    "total_cases": sheet.total_cases,
                })
                per_mailbox += 1
            next_url = page.get("@odata.nextLink")

    if not dry_run:
        # Re-load from disk and merge by source_message_id to avoid a
        # read-modify-write race when two scan calls overlap (a
        # symptom: the second call would clobber the first's appended
        # rows). The in-memory `existing` was loaded at the start of
        # THIS call and may now be stale.
        from inventory_tracker import load_production
        current = load_production()
        by_msg = {r.get("source_message_id"): r for r in current
                  if r.get("source_message_id")}
        # Records this call touched (added OR refreshed in place) live in
        # `existing` and are keyed by source_message_id in `touched_msg_ids`.
        # For msg_ids we touched, our in-memory version wins over the disk
        # version (e.g. refresh=true rewrote the lines with lot codes).
        # For msg_ids we did NOT touch but are present in BOTH `current` and
        # `existing`, the disk version wins (it may have been refreshed by a
        # concurrent scan). For msg_ids only in `existing`, those are new
        # additions — keep them.
        existing_by_msg = {r.get("source_message_id"): r for r in existing
                           if r.get("source_message_id")}
        for mid in touched_msg_ids:
            if mid in existing_by_msg:
                by_msg[mid] = existing_by_msg[mid]
        for r in existing:
            mid = r.get("source_message_id")
            if mid and mid not in by_msg:
                by_msg[mid] = r
            elif not mid:
                current.append(r)
        merged = [r for r in current if not r.get("source_message_id")] \
                 + list(by_msg.values())
        save_production(merged)

    return jsonify({
        "ok": True, "dry_run": dry_run,
        "lookback_days": lookback_days, "mailboxes": users,
        "messages_scanned": scanned,
        "messages_qualifying": qualifying_count,
        "ingested": ingested,
        "parse_errors": parse_errors,
        "records": brief_records,
    })


@production_bp.route("/api/admin/production/renormalize-varieties", methods=["POST"])
def api_admin_production_renormalize_varieties():
    """Re-run _normalize_variety on every stored production line.

    Useful after extending _VARIETY_ALIASES or _VARIETY_SHORTHAND so
    historical records (which kept their original best-effort variety
    label) get aggregated into the canonical buckets. Lines whose
    canonical changes also have their lot_number re-paired against
    the updated variety so the Distributors / Traceability tabs stay
    consistent.

    Body: { dry_run: bool }
    """
    from inventory_tracker import load_production, save_production
    from integrations.production_pdf_parser import _normalize_variety
    body = request.json or {}
    dry_run = bool(body.get("dry_run", False))
    records = load_production()
    changed_lines = 0
    changed_records = 0
    samples = []
    for r in records:
        any_changed = False
        for L in r.get("lines") or []:
            raw_v = L.get("raw_variety") or L.get("variety") or ""
            old_can = L.get("variety") or ""
            new_can, _recognized = _normalize_variety(raw_v)
            if new_can != old_can:
                if len(samples) < 25:
                    samples.append({
                        "raw": raw_v,
                        "old": old_can,
                        "new": new_can,
                        "po":  r.get("po_number") or "",
                        "date": r.get("production_date") or "",
                    })
                L["variety"] = new_can
                changed_lines += 1
                any_changed = True
        if any_changed:
            changed_records += 1
    if not dry_run:
        save_production(records)
    return jsonify({
        "ok": True,
        "dry_run": dry_run,
        "records_scanned": len(records),
        "records_changed": changed_records,
        "lines_changed": changed_lines,
        "samples": samples,
    })


@production_bp.route("/api/admin/production/reclassify", methods=["POST"])
def api_admin_production_reclassify():
    """Re-run the warehouse classifier on every stored production record.

    Useful after we add new entries to _WAREHOUSE_TO_CANONICAL or extend
    _classify_warehouse — historical records ingested under the old
    rules keep their warehouse/distributor unchanged unless we ask the
    parser to look at them again. This walks data/production.json,
    re-classifies each row using its existing warehouse_raw (or empty
    -> "In-House Inventory" for older sheets without a header), and
    persists the updates.

    Body: { dry_run: bool }
    """
    from inventory_tracker import load_production, save_production
    from integrations.production_pdf_parser import _classify_warehouse

    body = request.json or {}
    dry_run = bool(body.get("dry_run", False))
    records = load_production()
    changed_count = 0
    samples = []
    for r in records:
        raw = r.get("warehouse_raw") or ""
        new_wh, new_dist = _classify_warehouse(raw)
        old_wh   = r.get("warehouse") or ""
        old_dist = r.get("distributor") or ""
        if new_wh == old_wh and new_dist == old_dist:
            continue
        changed_count += 1
        if len(samples) < 25:
            samples.append({
                "warehouse_raw": raw,
                "old_warehouse": old_wh,
                "new_warehouse": new_wh,
                "old_distributor": old_dist,
                "new_distributor": new_dist,
            })
        if not dry_run:
            r["warehouse"] = new_wh
            r["distributor"] = new_dist
    if not dry_run:
        save_production(records)
    return jsonify({
        "ok": True, "dry_run": dry_run,
        "total_records": len(records),
        "changed": changed_count,
        "samples": samples,
    })


@production_bp.route("/api/production/<source_message_id>", methods=["DELETE"])
def api_production_delete(source_message_id):
    """Drop a production record by its source_message_id."""
    from inventory_tracker import load_production, save_production
    records = load_production()
    kept = [r for r in records if r.get("source_message_id") != source_message_id]
    removed = len(records) - len(kept)
    if removed:
        save_production(kept)
    return jsonify({"ok": True, "removed": removed})
