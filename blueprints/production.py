"""Production blueprint — production-run records: list, lots-by-pair FIFO,
summary, ingest, the MS365 production-PDF scan, variety renormalize /
reclassify admin ops, and delete. Extracted from app.py (refactor — see
REFACTOR_PLAN.md). Shared helpers come from core/."""
import os
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from core.cache import _AGG_CACHE, _data_sig
from core.errors import _log_exc, _safe_err
from core.util import with_data_lock
from inventory_tracker import load_inventory, load_usage

production_bp = Blueprint("production", __name__)


@production_bp.route("/api/production")
def api_production_list():
    """List production records. Optional query params: distributor,
    warehouse, since (ISO date). Returns newest-first."""
    from inventory_tracker import load_production
    from integrations.production_pdf_parser import ledger_warehouse_for
    records = load_production()
    dist = (request.args.get("distributor") or "").strip()
    wh   = (request.args.get("warehouse") or "").strip()
    since = (request.args.get("since") or "").strip()
    out = []
    for r in records:
        # Chefs Warehouse sheets say "Chefs Warehouse, NY"; the PO ledger says
        # "Bronx, NY". Expose the ledger name (read-time only, the stored
        # record is untouched) and accept either in the filter.
        lwh = ledger_warehouse_for(r.get("warehouse") or "", r.get("distributor") or "")
        if dist and (r.get("distributor") or "") != dist:
            continue
        if wh and wh not in ((r.get("warehouse") or ""), lwh):
            continue
        if since and (r.get("production_date") or "") < since:
            continue
        out.append(dict(r, ledger_warehouse=lwh))
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
    # canceled_pos / status overrides / CW store decide which lots are on a
    # canceled PO (excluded), so they are part of the cache signature too.
    sig = _data_sig("production.json", "usage.json", "inventory.json",
                    "canceled_pos.json", "po_status_overrides.json",
                    "chefs_warehouse_pos.json")
    _c = _AGG_CACHE.get("lots-by-pair")
    if _c and _c[0] == sig:
        return jsonify(_c[1])
    from inventory_tracker import (
        load_production, load_usage, load_inventory, compute_lot_fifo_state,
    )
    from integrations.production_store import canceled_po_keys
    records = load_production()
    usage = load_usage()
    inv_snapshot = load_inventory()
    canceled = canceled_po_keys()

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
            canceled_pos=canceled,
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
      message_id    Graph internetMessageId or RFC822 Message-Id. Optional:
                    without one a stable id is synthesized from the PDF's
                    content hash, so re-posting the same PDF is a no-op.
      received_at   ISO timestamp (optional)

    Returns 200 with {ok, status, record, error}. status is one of
    ingested | replaced | duplicate | superseded | tombstoned | parse_error
    (see integrations.production_store.ingest_production_record).
    parse_error is surfaced for image-only scan PDFs so the operator can
    re-request a text PDF.
    """
    import base64
    from datetime import datetime
    from integrations.production_pdf_parser import parse_production_pdf
    from integrations.production_store import (
        record_from_sheet, store_parsed, synth_message_id,
    )

    body = request.json or {}
    pdf_b64 = body.get("pdf_b64") or ""
    if not pdf_b64:
        return jsonify({"ok": False, "error": "pdf_b64 required"}), 400
    subject    = (body.get("subject") or "").strip()
    sender     = (body.get("sender") or "").strip()
    message_id = (body.get("message_id") or "").strip()
    received_at = (body.get("received_at") or "").strip() or datetime.now().isoformat()

    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc, "pdf_b64")}), 400
    # A record with no message id used to be re-appended by every scan
    # (2 -> 4 -> 8 copies); a content-hash id makes the same PDF the same row.
    if not message_id:
        message_id = synth_message_id(pdf_bytes)

    sheet = parse_production_pdf(pdf_bytes, subject=subject, received_at=received_at)
    record = record_from_sheet(sheet, message_id=message_id, subject=subject,
                               sender=sender, received_at=received_at)
    [(status, stored)] = store_parsed(
        [(record, getattr(sheet, "po_body", ""), getattr(sheet, "po_subject", ""))])
    out = {"ok": True, "status": status, "record": stored}
    if status == "parse_error":
        out["error"] = sheet.error
    return jsonify(out)


def _graph_page(url: str, token: str, *, attempts: int = 3, timeout: int = 45):
    """GET one Graph page, retrying transient failures.

    Why this exists: the production scan used to do a single bare urlopen and,
    on ANY exception, append "page N: parse error" and `break` out of that
    mailbox entirely. A momentary 429 or 503 on JD@ -- the only mailbox the
    production sheets arrive in -- therefore skipped the whole inbox for that
    run, reported a message that named the wrong layer (nothing had been
    parsed yet), and logged nothing at all server-side. That combination is
    why a ten-week ingestion gap was invisible.

    Retries 429/5xx/timeouts with backoff, honouring Retry-After. Raises
    RuntimeError with a CLASSIFIED, exception-text-free message on give-up;
    the caller logs the real exception to stderr.
    """
    import time as _time
    import urllib.error as _uerr
    import urllib.request as _ureq
    last = ""
    for attempt in range(1, attempts + 1):
        req = _ureq.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        req.add_header("ConsistencyLevel", "eventual")
        try:
            with _ureq.urlopen(req, timeout=timeout) as resp:
                return _json_loads(resp.read().decode("utf-8"))
        except _uerr.HTTPError as exc:
            last = f"HTTP {exc.code}"
            retryable = exc.code == 429 or 500 <= exc.code < 600
            wait = 0.0
            if retryable:
                try:
                    wait = float(exc.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0.0
        except Exception as exc:  # noqa: BLE001  (timeout, DNS, decode)
            last = type(exc).__name__
            retryable = True
            wait = 0.0
        if not retryable or attempt == attempts:
            raise RuntimeError(last)
        _time.sleep(wait or min(2 ** attempt, 8))
    raise RuntimeError(last or "unknown")


def _json_loads(txt):
    import json as _json
    return _json.loads(txt)


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
        from integrations.production_store import (
            load_tombstones, record_from_sheet, seen_message_ids, store_parsed,
        )
        from inventory_tracker import load_production
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
    # Kept apart from parse_errors on purpose. A fetch failure means messages
    # went UNREAD; a parse failure means a sheet was read and couldn't be
    # understood. Lumping them together is what let "page 1: parse error"
    # masquerade as a parsing problem for ten weeks.
    fetch_errors = []
    # Production emails whose attachments held no PDF. This used to be a bare
    # `continue`, so a sheet sent as .xlsx only vanished with no trace.
    skipped_no_pdf = []
    brief_records = []
    # Everything already accounted for: stored ids, emails a newer sheet
    # replaced, and deleted (tombstoned) records -- a delete used to be undone
    # by the next 14-day-lookback scan.
    seen_msg_ids = seen_message_ids(load_production(), load_tombstones())
    # Parsed sheets are collected here and stored in ONE locked write after
    # all Graph I/O is done (never hold data_lock across the network).
    to_store = []   # [(record, po_body, po_subject, brief)]

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
                page = _graph_page(next_url, token)
            except Exception as exc:  # noqa: BLE001
                # Say which mailbox stopped, at which page, and why -- and
                # make clear the REST of that inbox went unread, because a
                # silent partial scan reads as "nothing new".
                _log_exc(exc, f"production scan {upn} page {pages}")
                fetch_errors.append(
                    f"{upn}: aborted at page {pages} after retries "
                    f"({_safe_err(exc, 'graph fetch')}) - the rest of this "
                    f"mailbox was NOT scanned on this run."
                )
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
                    _log_exc(exc, f"production scan list-att {subject[:60]!r}")
                    fetch_errors.append(
                        f"{subject[:60]!r}: could not list attachments "
                        f"({_safe_err(exc, 'graph fetch')})")
                    continue
                pdf_atts = [a for a in apage.get("value", [])
                            if (a.get("name") or "").lower().endswith(".pdf")
                            or (a.get("contentType") or "").lower() == "application/pdf"]
                if not pdf_atts:
                    names = [a.get("name") or "?" for a in apage.get("value", [])]
                    skipped_no_pdf.append({
                        "subject": subject[:90],
                        "received_at": m.get("receivedDateTime") or "",
                        "attachments": names[:6],
                    })
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
                    _log_exc(exc, f"production scan fetch-att {subject[:60]!r}")
                    fetch_errors.append(
                        f"{subject[:60]!r}: could not download the PDF "
                        f"({_safe_err(exc, 'graph fetch')})")
                    continue
                received_at = m.get("receivedDateTime") or ""
                sheet = parse_production_pdf(pdf_bytes, subject=subject,
                                             received_at=received_at)
                if sheet.error and not sheet.lines:
                    parse_errors.append(f"{subject[:60]!r}: {sheet.error}")
                    if dry_run:
                        per_mailbox += 1
                        continue
                    # Persist a stub so the operator sees it in the UI
                    record = record_from_sheet(sheet, message_id=msg_id, subject=subject,
                                               sender=sender, received_at=received_at)
                    seen_msg_ids.add(msg_id)
                    to_store.append((record, "", "", {
                        "subject": subject, "po_number": "", "warehouse": "",
                        "total_cases": 0, "parse_error": sheet.error}))
                    per_mailbox += 1
                    continue
                if dry_run:
                    per_mailbox += 1
                    continue
                record = record_from_sheet(sheet, message_id=msg_id, subject=subject,
                                           sender=sender, received_at=received_at)
                seen_msg_ids.add(msg_id)
                to_store.append((record, sheet.po_body, sheet.po_subject, {
                    "subject": subject, "po_number": sheet.po_number,
                    "warehouse": sheet.warehouse, "production_date": sheet.production_date,
                    "total_cases": sheet.total_cases,
                }))
                per_mailbox += 1
            next_url = page.get("@odata.nextLink")

    if not dry_run and to_store:
        # One locked load -> ingest -> save against the CURRENT file (a
        # concurrent webhook or admin edit is never clobbered). The old
        # end-of-run merge re-appended every record that had no message id,
        # doubling it on each daily run.
        results = store_parsed([(rec, b, s) for rec, b, s, _brief in to_store],
                               refresh=refresh)
        for (status, stored), (_rec, _b, _s, brief) in zip(results, to_store):
            if status in ("ingested", "replaced", "refreshed"):
                ingested += 1
            if status in ("ingested", "replaced", "refreshed", "parse_error"):
                brief = dict(brief, status=status)
                if stored and stored.get("po_number"):
                    brief["po_number"] = stored["po_number"]
                brief_records.append(brief)

    return jsonify({
        "ok": True, "dry_run": dry_run,
        "lookback_days": lookback_days, "mailboxes": users,
        "messages_scanned": scanned,
        "messages_qualifying": qualifying_count,
        "ingested": ingested,
        "parse_errors": parse_errors,
        "fetch_errors": fetch_errors,
        "skipped_no_pdf": skipped_no_pdf,
        "complete": not fetch_errors,
        "records": brief_records,
    })


@production_bp.route("/api/admin/production/renormalize-varieties", methods=["POST"])
@with_data_lock
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
    from integrations.production_pdf_parser import (
        _normalize_variety, is_non_item_label, split_assorted,
    )
    body = request.json or {}
    dry_run = bool(body.get("dry_run", False))
    records = load_production()
    changed_lines = 0
    changed_records = 0
    dropped_footer_lines = 0
    dropped_footer_cases = 0.0
    expanded_assorted = 0
    samples = []
    for r in records:
        any_changed = False
        record_dropped_footer = False
        new_lines = []

        # DERIVED lines (today: the assorted split) are re-derived as a GROUP
        # from their shared original, never re-resolved individually.
        #
        # Re-resolving them individually is precisely the bug this replaced:
        # raw_variety on every one of them is still "ASSORTED", so the
        # "canonical changed" branch happily rewrote all four back to
        # Assorted and destroyed the split -- 96 lines on 2026-08-28.
        # Re-splitting the group's own total instead is idempotent AND
        # self-healing: it reproduces the same split whether the lines are
        # intact or collapsed.
        derived_groups: dict = {}
        for L in r.get("lines") or []:
            src = L.get("derived_from")
            if src:
                derived_groups.setdefault(src, []).append(L)
        for src, group in derived_groups.items():
            total = int(sum(float(x.get("cs_count") or 0) for x in group))
            rebuilt = [
                {**group[0], "variety": v, "cs_count": part,
                 "raw_variety": group[0].get("raw_variety") or src,
                 "derived_from": src}
                for v, part in split_assorted(total) if part
            ]
            before = [(x.get("variety"), x.get("cs_count")) for x in group]
            after = [(x.get("variety"), x.get("cs_count")) for x in rebuilt]
            if before != after:
                changed_lines += len(rebuilt)
                any_changed = True
                if len(samples) < 30:
                    samples.append({"raw": src, "old": before[:2],
                                    "new": f"re-split {total} cs",
                                    "po": r.get("po_number") or ""})
            new_lines.extend(rebuilt)

        for L in r.get("lines") or []:
            if L.get("derived_from"):
                continue          # handled as a group above
            raw_v = L.get("raw_variety") or L.get("variety") or ""
            old_can = L.get("variety") or ""

            # A "TOTAL" footer row is a rollup of the sheet, not an item.
            # Stored as a line it double-counts the whole sheet.
            if is_non_item_label(raw_v):
                dropped_footer_lines += 1
                dropped_footer_cases += float(L.get("cs_count") or 0)
                record_dropped_footer = True
                any_changed = True
                if len(samples) < 30:
                    samples.append({"raw": raw_v, "old": old_can,
                                    "new": "(dropped - footer row)",
                                    "cs": L.get("cs_count"),
                                    "po": r.get("po_number") or ""})
                continue

            new_can, _recognized = _normalize_variety(raw_v)

            # Assorted expands into ASSORTED_SPLIT. Already-derived lines are
            # left alone so re-running stays idempotent.
            if new_can == "Assorted":
                cs = int(float(L.get("cs_count") or 0))
                for v, part in split_assorted(cs):
                    if not part:
                        continue
                    new_lines.append({**L, "variety": v, "cs_count": part,
                                      "raw_variety": raw_v,
                                      "derived_from": raw_v.strip().upper()})
                expanded_assorted += 1
                changed_lines += 1
                any_changed = True
                if len(samples) < 30:
                    samples.append({"raw": raw_v, "old": old_can,
                                    "new": f"split {cs} cs across "
                                           f"{len(split_assorted(cs))} varieties",
                                    "po": r.get("po_number") or ""})
                continue

            if new_can != old_can:
                if len(samples) < 30:
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
            new_lines.append(L)

        # Re-resolve the STORED unmapped list on every record, changed or
        # not. It is a snapshot taken at ingest, so a record whose lines were
        # already canonical kept flagging labels that a later alias had long
        # since fixed -- 75 of the 85 flags on 2026-08-28 were stale, which
        # buries the handful that are real.
        fresh_unmapped = sorted({
            (x.get("raw_variety") or "") for x in new_lines
            if not _normalize_variety(x.get("raw_variety") or "")[1]
        } - {""})
        if fresh_unmapped != (r.get("unmapped_varieties") or []):
            r["unmapped_varieties"] = fresh_unmapped
            any_changed = True

        if any_changed:
            r["lines"] = new_lines
            # The header total is the sheet's OWN printed figure -- keep it,
            # so a line the parser missed still shows up as a discrepancy
            # rather than being silently papered over. Recompute only when
            # there was no header total, or when THIS record shed a footer
            # row (whose cases were inflating it).
            if not r.get("total_cases") or record_dropped_footer:
                r["total_cases"] = int(
                    sum(float(x.get("cs_count") or 0) for x in new_lines))
            changed_records += 1

    if not dry_run:
        save_production(records)
    return jsonify({
        "ok": True,
        "dry_run": dry_run,
        "records_scanned": len(records),
        "records_changed": changed_records,
        "lines_changed": changed_lines,
        "assorted_lines_expanded": expanded_assorted,
        "footer_lines_dropped": dropped_footer_lines,
        "footer_cases_removed": round(dropped_footer_cases, 2),
        "samples": samples,
    })


@production_bp.route("/api/admin/production/reclassify", methods=["POST"])
@with_data_lock
def api_admin_production_reclassify():
    """Re-run the warehouse classifier on every stored production record.

    Useful after we add new entries to _WAREHOUSE_TO_CANONICAL or extend
    _classify_warehouse — historical records ingested under the old
    rules keep their warehouse/distributor unchanged unless we ask the
    parser to look at them again. This walks data/production.json,
    re-classifies each row using its existing warehouse_raw and -- when that
    is blank or unmatched -- the DC named in its subject line (Houston
    393072B2 was stored as "US FOODS HOUSTON"; 64 sheets / 14,285 cs had a
    blank warehouse but a DC in the subject), and persists the updates.
    A blank label with no DC in the subject still means In-House Inventory.

    Body: { dry_run: bool }
    """
    from inventory_tracker import load_production, save_production
    from integrations.production_pdf_parser import classify_warehouse_with_subject

    body = request.json or {}
    dry_run = bool(body.get("dry_run", False))
    records = load_production()
    changed_count = 0
    samples = []
    for r in records:
        raw = r.get("warehouse_raw") or ""
        new_wh, new_dist = classify_warehouse_with_subject(
            raw, r.get("source_subject") or "")
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


@production_bp.route("/api/admin/production/repair", methods=["POST"])
def api_admin_production_repair():
    """One idempotent pass that re-judges every stored production record with
    today's rules (dry_run defaults to TRUE -- pass {"dry_run": false} to
    write):

      * warehouse from the subject's DC when blank/unmatched (K2);
      * PDF-header PO vs subject PO resolved against the PO ledger (C3),
        e.g. the CWC 7/1 sheet moves from CWFL's 1135526 to 1135525;
      * production date = the most common plausible lot date (C1), e.g.
        Riviera 014511751616 moves from 2026-03-10 to 2026-09-10;
      * duplicate sheets for the same (warehouse, PO, date) collapse to the
        newest email (C2), e.g. Ocala 054511751617 back to 112 cs.

    Returns per-record before/after, the removed duplicates, and a summary.
    A real run writes one inventory-audit row.
    """
    from datetime import datetime
    import inventory_tracker as it
    from integrations.production_store import (
        ledger_po_index, repair_production_records,
    )
    body = request.json or {}
    dry_run = body.get("dry_run", True) is not False
    index = ledger_po_index()          # outside the lock (reads only)
    with it.data_lock():
        records = it.load_production()
        new_records, changes, removed, summary = repair_production_records(records, index)
        if not dry_run and (changes or removed):
            it.save_production(new_records)
            it.append_inventory_audit([{
                "ts": datetime.now().isoformat(), "event_type": "production_repair",
                "name": "production.json", "warehouse": "",
                "old_quantity": len(records), "new_quantity": len(new_records),
                "delta": len(new_records) - len(records),
                "source": "admin/production-repair",
                "detail": summary,
            }])
    return jsonify({"ok": True, "dry_run": dry_run, "summary": summary,
                    "changes": changes, "removed_duplicates": removed})


@production_bp.route("/api/production/<source_message_id>", methods=["DELETE"])
@with_data_lock
def api_production_delete(source_message_id):
    """Drop a production record by its source_message_id.

    Leaves a tombstone for the email (and any it had replaced): the daily
    scan's 14-day lookback used to re-ingest a deleted sheet that same night.
    """
    from datetime import datetime
    from inventory_tracker import load_production, save_production
    from integrations.production_store import load_tombstones, save_tombstones
    records = load_production()
    gone = [r for r in records if r.get("source_message_id") == source_message_id]
    kept = [r for r in records if r.get("source_message_id") != source_message_id]
    removed = len(records) - len(kept)
    if removed:
        save_production(kept)
        stones = load_tombstones()
        now = datetime.now().isoformat()
        for r in gone:
            for mid in [source_message_id] + list(r.get("replaced") or []):
                if mid:
                    stones[mid] = {"deleted_at": now,
                                   "po_number": r.get("po_number") or "",
                                   "warehouse": r.get("warehouse") or "",
                                   "production_date": r.get("production_date") or ""}
        save_tombstones(stones)
    return jsonify({"ok": True, "removed": removed})
