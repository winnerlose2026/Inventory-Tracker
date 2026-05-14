#!/usr/bin/env python3
"""Inventory Tracker - Flask Web GUI"""

import io
import os
from flask import Flask, jsonify, request, render_template, send_file, make_response
from inventory_tracker import (
    load_inventory, save_inventory, load_usage, save_usage,
    add_item, update_item, record_usage, restock, remove_item,
    reverse_usage,
)
from datetime import datetime

app = Flask(__name__)


# ---------------------------------------------------------------------------
# CORS — lets a Shopify-hosted page (or any allowed origin) call /api/*.
# Set ALLOWED_ORIGINS to a comma-separated list, e.g.
#   ALLOWED_ORIGINS=https://your-store.myshopify.com,https://your-store.com
# Leave unset during local development.
# ---------------------------------------------------------------------------

def _allowed_origins() -> list[str]:
    raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
    return [o.strip() for o in raw.split(",") if o.strip()]


def _origin_allowed(origin: str) -> bool:
    if not origin:
        return False
    allowed = _allowed_origins()
    if not allowed:
        return False
    if "*" in allowed:
        return True
    return origin in allowed


@app.after_request
def _apply_cors(response):
    origin = request.headers.get("Origin", "")
    if _origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Inventory-Token"
        response.headers["Access-Control-Max-Age"] = "600"
    return response


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def _cors_preflight(_any):
    # Short-circuit the preflight; _apply_cors adds the headers.
    return make_response("", 204)


# ---------------------------------------------------------------------------
# Write-endpoint auth. When INVENTORY_API_TOKEN is set, every write route
# requires `X-Inventory-Token: <value>`. Unset (the default) = open, matches
# the original local-only behaviour.
# ---------------------------------------------------------------------------

def _require_write_token():
    expected = os.environ.get("INVENTORY_API_TOKEN", "").strip()
    if not expected:
        return None
    got = (request.headers.get("X-Inventory-Token") or "").strip()
    if got != expected:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None


@app.before_request
def _gate_writes():
    if request.method in ("POST", "PUT", "DELETE") and request.path.startswith("/api/"):
        denial = _require_write_token()
        if denial is not None:
            return denial


@app.route("/api/auth/check")
def api_auth_check():
    """Let the widget ask 'does this backend require a token, and is mine good?'"""
    expected = os.environ.get("INVENTORY_API_TOKEN", "").strip()
    if not expected:
        return jsonify({"required": False, "authorized": True})
    got = (request.headers.get("X-Inventory-Token") or "").strip()
    return jsonify({"required": True, "authorized": got == expected})


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    # When INVENTORY_AUTO_AUTH is on, hand the admin token to the page so the
    # browser doesn't have to prompt on each fresh login / incognito window.
    # Off by default — turning it on means "anyone who can load this page is
    # treated as admin." Acceptable for a single-tenant deployment behind an
    # obscure URL; not for a shared one. Accepted truthy values:
    # 1, true, yes, on (case-insensitive).
    expected = os.environ.get("INVENTORY_API_TOKEN", "").strip()
    auto_flag = os.environ.get("INVENTORY_AUTO_AUTH", "").strip().lower()
    auto_on = auto_flag in ("1", "true", "yes", "on")
    auto_token = expected if (expected and auto_on) else ""
    return render_template("index.html", auto_admin_token=auto_token)


# ---------------------------------------------------------------------------
# API – Inventory
# ---------------------------------------------------------------------------

def _enrich_on_order(item: dict) -> dict:
    """Add convenience fields summarising pending on_order entries."""
    pending = item.get("on_order") or []
    total = round(sum(float(p.get("qty") or 0) for p in pending), 2)
    etas = [p.get("eta", "") for p in pending if p.get("eta")]
    next_eta = min(etas) if etas else ""
    item["on_order_qty"] = total
    item["on_order_next_eta"] = next_eta
    return item


@app.route("/api/inventory")
def api_inventory():
    inv = load_inventory()
    return jsonify([_enrich_on_order(dict(v)) for v in inv.values()])


@app.route("/api/inventory", methods=["POST"])
def api_add():
    d = request.json
    add_item(
        name=d["name"],
        quantity=float(d["quantity"]),
        unit=d["unit"],
        category=d.get("category", "general"),
        low_stock_threshold=float(d.get("low_stock_threshold", 5)),
        price=float(d.get("price", 0)),
        distributor=d.get("distributor", ""),
        warehouse=d.get("warehouse", ""),
        case_cost=float(d.get("case_cost", 0)),
        case_size=int(d.get("case_size", 0) or 0),
        weekly_usage=float(d.get("weekly_usage", 0)),
    )
    return jsonify({"ok": True})


@app.route("/api/inventory/<path:name>", methods=["PUT"])
def api_update(name):
    d = request.json
    update_item(
        name,
        quantity=float(d["quantity"]) if "quantity" in d else None,
        unit=d.get("unit"),
        category=d.get("category"),
        low_stock_threshold=float(d["low_stock_threshold"]) if "low_stock_threshold" in d else None,
        price=float(d["price"]) if "price" in d else None,
        distributor=d.get("distributor"),
        warehouse=d.get("warehouse"),
        case_cost=float(d["case_cost"]) if "case_cost" in d else None,
        case_size=int(d["case_size"]) if "case_size" in d else None,
        weekly_usage=float(d["weekly_usage"]) if "weekly_usage" in d else None,
    )
    return jsonify({"ok": True})


@app.route("/api/inventory/<path:name>", methods=["DELETE"])
def api_remove(name):
    remove_item(name)
    return jsonify({"ok": True})


@app.route("/api/on-order/ship-date", methods=["POST"])
def api_on_order_ship_date():
    """Set (or clear) a ship_date on all pending on_order entries for a PO.

    Body:
      po_number   (required)
      ship_date   ISO date or empty/null to clear
      item_key    (optional) limit the update to a single SKU; default
                  is to update every SKU's on_order list that carries
                  this PO. Per-PO is the common case because a PO ships
                  as a whole — every line item arrives together.

    Behavior:
      - Stores ship_date on each matching entry.
      - Computes arrival_date = ship_date + 7 days and stores it.
      - Clearing ship_date (empty string / null) also clears
        arrival_date, returning the entry to the default 30-day-from-
        ordered_at rollover.
      - inventory.load_inventory()'s rollover pass picks up the new
        arrival_date on its next call, so a ship_date that's already in
        the past promotes the entry immediately (after the +7).
    """
    from datetime import datetime, timedelta
    from inventory_tracker import load_inventory, save_inventory

    body = request.json or {}
    po_number = (body.get("po_number") or "").strip()
    if not po_number:
        return jsonify({"ok": False, "error": "po_number required"}), 400
    item_key = (body.get("item_key") or "").strip().lower() or None
    ship_raw = body.get("ship_date")

    # Empty value clears.
    if ship_raw is None or (isinstance(ship_raw, str) and not ship_raw.strip()):
        ship_iso = ""
        arrival_iso = ""
    else:
        try:
            ship_dt = datetime.fromisoformat(str(ship_raw).strip())
        except ValueError:
            return jsonify({
                "ok": False,
                "error": "ship_date must be ISO 8601 (YYYY-MM-DD or full datetime)",
            }), 400
        ship_iso = ship_dt.isoformat()
        arrival_iso = (ship_dt + timedelta(days=7)).isoformat()

    inv = load_inventory()
    updated = 0
    touched_items = []
    for key, item in inv.items():
        if item_key and key != item_key:
            continue
        pending = item.get("on_order") or []
        for entry in pending:
            if (entry.get("po_number") or "") != po_number:
                continue
            entry["ship_date"] = ship_iso
            entry["arrival_date"] = arrival_iso
            updated += 1
            if item.get("name") not in touched_items:
                touched_items.append(item.get("name") or key)
    save_inventory(inv)
    return jsonify({
        "ok": True,
        "po_number": po_number,
        "ship_date": ship_iso,
        "arrival_date": arrival_iso,
        "entries_updated": updated,
        "items": touched_items,
    })


@app.route("/api/admin/remove-po", methods=["POST"])
def api_admin_remove_po():
    """Drop all pending on_order entries matching a po_number.

    Used to manually retire a PO whose source email has been deleted or
    archived (so the auto-supersede via re-scan can't reach it). The
    operation does NOT touch already-rolled-over quantity or usage
    entries — only items still sitting in item["on_order"].
    """
    body = request.json or {}
    po_number = (body.get("po_number") or "").strip()
    if not po_number:
        return jsonify({"ok": False, "error": "po_number required"}), 400
    from inventory_tracker import load_inventory, save_inventory
    inv = load_inventory()
    removed = 0
    affected = []
    for key, item in inv.items():
        pending = item.get("on_order") or []
        kept = [e for e in pending if (e.get("po_number") or "") != po_number]
        if len(kept) != len(pending):
            removed += len(pending) - len(kept)
            affected.append(item.get("name", key))
        item["on_order"] = kept
    save_inventory(inv)
    return jsonify({
        "ok": True,
        "po_number": po_number,
        "removed_entries": removed,
        "affected_items": affected,
    })


# ---------------------------------------------------------------------------
# API – Daily Production
# ---------------------------------------------------------------------------
# Production sheets are separate from inventory. Each record describes
# what was baked for a particular PO on a particular day, parsed from
# the Daily Production Sheet PDF that the production team emails out.

@app.route("/api/production")
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


@app.route("/api/production/summary")
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


@app.route("/api/production/ingest", methods=["POST"])
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
        return jsonify({"ok": False, "error": f"bad pdf_b64: {exc}"}), 400

    sheet = parse_production_pdf(pdf_bytes, subject=subject)

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


@app.route("/api/production/scan", methods=["POST"])
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
        return jsonify({"ok": False, "error": f"import failed: {exc}",
                        "traceback": _tb.format_exc()[-1500:]}), 500

    body = request.json or {}
    try:
        lookback_days = int(body.get("lookback_days") or 365)
    except (TypeError, ValueError):
        lookback_days = 365
    try:
        max_messages = int(body.get("max_messages") or 200)
    except (TypeError, ValueError):
        max_messages = 200
    max_messages = max(1, min(max_messages, 2000))
    dry_run = bool(body.get("dry_run", False))

    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    since_iso = since.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    client = EmailInboxClient()
    try:
        token = client._ms365_token()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"ms365 token failed: {exc}"}), 500

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

    for upn in users:
        user = urllib.parse.quote(upn)
        qs = urllib.parse.urlencode({
            "$top": "100",
            "$select": ("id,subject,from,toRecipients,receivedDateTime,"
                        "hasAttachments,internetMessageId"),
            "$filter": f"hasAttachments eq true and receivedDateTime ge {since_iso}",
        })
        next_url = f"{GRAPH_BASE}/users/{user}/mailFolders/{folder}/messages?{qs}"
        pages = 0
        per_mailbox = 0
        while next_url and pages < 40 and per_mailbox < max_messages:
            pages += 1
            try:
                raw, _ = client._graph_get(next_url, token)
                page = _json.loads(raw.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                parse_errors.append(f"{upn} page {pages}: {exc}")
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
                if msg_id and msg_id in seen_msg_ids:
                    continue  # already ingested
                # Pull the attachments list
                att_url = f"{GRAPH_BASE}/users/{user}/messages/{m.get('id')}/attachments"
                try:
                    araw, _ = client._graph_get(att_url, token)
                    apage = _json.loads(araw.decode("utf-8"))
                except Exception as exc:  # noqa: BLE001
                    parse_errors.append(f"{subject[:60]!r}: list-att failed: {exc}")
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
                    parse_errors.append(f"{subject[:60]!r}: fetch-att failed: {exc}")
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
                existing.append(record)
                seen_msg_ids.add(msg_id)
                ingested += 1
                brief_records.append({
                    "subject": subject, "po_number": sheet.po_number,
                    "warehouse": sheet.warehouse, "production_date": sheet.production_date,
                    "total_cases": sheet.total_cases,
                })
                per_mailbox += 1
            next_url = page.get("@odata.nextLink")

    if not dry_run:
        save_production(existing)

    return jsonify({
        "ok": True, "dry_run": dry_run,
        "lookback_days": lookback_days, "mailboxes": users,
        "messages_scanned": scanned,
        "messages_qualifying": qualifying_count,
        "ingested": ingested,
        "parse_errors": parse_errors,
        "records": brief_records,
    })


@app.route("/api/production/<source_message_id>", methods=["DELETE"])
def api_production_delete(source_message_id):
    """Drop a production record by its source_message_id."""
    from inventory_tracker import load_production, save_production
    records = load_production()
    kept = [r for r in records if r.get("source_message_id") != source_message_id]
    removed = len(records) - len(kept)
    if removed:
        save_production(kept)
    return jsonify({"ok": True, "removed": removed})


# ---------------------------------------------------------------------------
# API – $PLH report (production revenue per labor hour at the bakery)
# ---------------------------------------------------------------------------
# Per-case sell prices (revenue side):
#   US Foods           $27.00
#   Cheney Brothers    $26.50
#   anything else      $29.50   (Chefs Warehouse, unassigned, etc.)
# Default labor rate $17/hr used to back-fill `dollars` on a labor entry
# that only carries `hours`. PLH = revenue / labor_hours.

CASE_PRICE_BY_DISTRIBUTOR = {
    "US Foods":        27.00,
    "Cheney Brothers": 26.50,
}
CASE_PRICE_DEFAULT = 29.50
LABOR_RATE_DEFAULT = 17.00


def _case_price_for(distributor: str) -> float:
    return CASE_PRICE_BY_DISTRIBUTOR.get((distributor or "").strip(),
                                         CASE_PRICE_DEFAULT)


def _plh_bucket_keys(grain: str):
    """Return [(key, start_iso, end_iso, label)] for the buckets in scope.

    grain = "week"     -> last 4 ISO weeks ending this week (Mon-Sun)
    grain = "month"    -> the 3 months of the current calendar quarter
    grain = "quarter"  -> the 4 quarters of the current calendar year
    """
    from datetime import datetime, timedelta
    today = datetime.now().date()

    if grain == "month":
        # Quarter the current month belongs to (1-3, 4-6, 7-9, 10-12)
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        out = []
        for i in range(3):
            mm = q_start_month + i
            start = datetime(today.year, mm, 1).date()
            nxt_mm = mm + 1
            nxt_yy = today.year
            if nxt_mm > 12:
                nxt_mm = 1
                nxt_yy = today.year + 1
            end = (datetime(nxt_yy, nxt_mm, 1).date() - timedelta(days=1))
            key   = start.strftime("%Y-%m")
            label = start.strftime("%b %Y")
            out.append((key, start.isoformat(), end.isoformat(), label))
        return out

    if grain == "quarter":
        out = []
        for q in range(1, 5):
            start_mm = (q - 1) * 3 + 1
            start = datetime(today.year, start_mm, 1).date()
            end_mm = start_mm + 3
            end_yy = today.year
            if end_mm > 12:
                end_mm = 1
                end_yy = today.year + 1
            end = (datetime(end_yy, end_mm, 1).date() - timedelta(days=1))
            key   = f"{today.year}-Q{q}"
            label = f"Q{q} {today.year}"
            out.append((key, start.isoformat(), end.isoformat(), label))
        return out

    # default: weekly. 4 ISO weeks ending this week (Mon-Sun)
    day = today.weekday()        # Mon=0..Sun=6
    this_monday = today - timedelta(days=day)
    out = []
    for i in range(3, -1, -1):    # 3 weeks ago -> this week
        wk_start = this_monday - timedelta(days=7 * i)
        wk_end   = wk_start + timedelta(days=6)
        iso = wk_start.isocalendar()
        key   = f"{iso.year:04d}-W{iso.week:02d}"
        label = f"Week of {wk_start.strftime('%b %d')}"
        out.append((key, wk_start.isoformat(), wk_end.isoformat(), label))
    return out


def _date_in_range(d: str, start: str, end: str) -> bool:
    if not d:
        return False
    d = d[:10]
    return start <= d <= end


@app.route("/api/report/plh")
def api_report_plh():
    """Production revenue, labor hours, and $PLH per time bucket."""
    from inventory_tracker import load_production, load_labor
    grain = (request.args.get("grain") or "week").lower()
    buckets = _plh_bucket_keys(grain)
    prod = load_production()
    labor = load_labor()

    out = []
    for key, start, end, label in buckets:
        bucket = {
            "key": key, "label": label, "start": start, "end": end,
            "total_cs": 0,
            "revenue_dollars": 0.0,
            "by_distributor": {},
            "labor_hours": 0.0,
            "labor_dollars": 0.0,
            "plh": None,
        }
        for r in prod:
            if not _date_in_range(r.get("production_date") or "", start, end):
                continue
            dist = r.get("distributor") or ""
            price = _case_price_for(dist)
            for line in (r.get("lines") or []):
                cs = int(line.get("cs_count") or 0)
                bucket["total_cs"] += cs
                bucket["revenue_dollars"] += cs * price
                bdist = bucket["by_distributor"].setdefault(
                    dist or "Other", {"cs": 0, "revenue_dollars": 0.0})
                bdist["cs"] += cs
                bdist["revenue_dollars"] += cs * price
        for e in labor:
            if not _date_in_range(e.get("date") or "", start, end):
                continue
            bucket["labor_hours"]   += float(e.get("hours") or 0)
            bucket["labor_dollars"] += float(e.get("dollars") or 0)
        # If we have hours but no dollars on any entry, impute via the default
        # rate so the cost picture is at least directionally right.
        if bucket["labor_hours"] and not bucket["labor_dollars"]:
            bucket["labor_dollars"] = bucket["labor_hours"] * LABOR_RATE_DEFAULT
            bucket["labor_dollars_imputed"] = True
        if bucket["labor_hours"]:
            bucket["plh"] = bucket["revenue_dollars"] / bucket["labor_hours"]
        out.append(bucket)

    return jsonify({
        "grain": grain,
        "case_prices": {
            "US Foods": 27.00, "Cheney Brothers": 26.50, "default": 29.50,
        },
        "labor_rate_default": LABOR_RATE_DEFAULT,
        "buckets": out,
    })


@app.route("/api/admin/labor/ingest", methods=["POST"])
def api_admin_labor_ingest():
    """Append or replace bakery labor entries.

    Body:
      entries:  [{date: YYYY-MM-DD, hours: float, dollars?: float, source?: str}]
      replace:  bool — when true, REPLACE all entries from the same `source`
                with the new set; when false (default), append + dedupe by
                (date, source) keeping the new entry.

    No Toast call here — this is the "feed me labor data from any source"
    endpoint. A separate script can pull from Toast and POST. CSV upload
    or manual entry can use this same endpoint.
    """
    from inventory_tracker import load_labor, save_labor
    body = request.json or {}
    incoming = body.get("entries") or []
    replace = bool(body.get("replace", False))
    if not incoming:
        return jsonify({"ok": False, "error": "entries required"}), 400

    existing = load_labor()
    sources_in_incoming = {(e.get("source") or "manual") for e in incoming}
    if replace:
        existing = [e for e in existing if (e.get("source") or "manual")
                    not in sources_in_incoming]

    # Dedupe by (date, source). Newer wins.
    by_key = {}
    for e in existing:
        by_key[(e.get("date"), e.get("source") or "manual")] = e
    for e in incoming:
        d = (e.get("date") or "").strip()
        if not d:
            continue
        rec = {
            "date":    d,
            "hours":   float(e.get("hours") or 0),
            "dollars": float(e.get("dollars") or 0),
            "source":  e.get("source") or "manual",
            "ingested_at": datetime_now_iso(),
        }
        by_key[(rec["date"], rec["source"])] = rec
    merged = list(by_key.values())
    save_labor(merged)
    return jsonify({"ok": True, "total_entries": len(merged),
                    "added_or_updated": len(incoming)})


def datetime_now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat()



# ---------------------------------------------------------------------------
# API – Usage
# ---------------------------------------------------------------------------

@app.route("/api/use", methods=["POST"])
def api_use():
    d = request.json
    record_usage(d["name"], float(d["amount"]), d.get("note", ""))
    return jsonify({"ok": True})


@app.route("/api/restock", methods=["POST"])
def api_restock():
    d = request.json
    restock(d["name"], float(d["amount"]), d.get("note", ""))
    return jsonify({"ok": True})


@app.route("/api/usage")
def api_usage():
    name = request.args.get("name")
    limit = int(request.args.get("limit", 50))
    usage = load_usage()
    if name:
        key = name.lower().strip()
        usage = [e for e in usage if e["item_key"] == key]
    usage = list(reversed(usage))[:limit]
    return jsonify(usage)


@app.route("/api/usage/reverse", methods=["POST"])
def api_usage_reverse():
    """Undo a single usage/restock entry by its timestamp."""
    d = request.json or {}
    ts = (d.get("timestamp") or "").strip()
    if not ts:
        return jsonify({"ok": False, "error": "timestamp required"}), 400
    result = reverse_usage(ts)
    return jsonify(result)


# ---------------------------------------------------------------------------
# API – Report
# ---------------------------------------------------------------------------

@app.route("/api/report")
def api_report():
    inv = load_inventory()
    usage = load_usage()

    total_value = sum(i["quantity"] * i["price"] for i in inv.values())
    total_items = len(inv)
    low_stock = [i for i in inv.values() if i["quantity"] <= i["low_stock_threshold"]]

    consumed: dict = {}
    restocked: dict = {}
    for e in usage:
        key = e["item_key"]
        if e["amount"] < 0:
            restocked[key] = restocked.get(key, 0) + abs(e["amount"])
        else:
            consumed[key] = consumed.get(key, 0) + e["amount"]

    top_consumed = sorted(
        [{"key": k, "name": inv.get(k, {}).get("name", k),
          "unit": inv.get(k, {}).get("unit", ""), "total": v}
         for k, v in consumed.items()],
        key=lambda x: x["total"], reverse=True
    )[:10]

    top_restocked = sorted(
        [{"key": k, "name": inv.get(k, {}).get("name", k),
          "unit": inv.get(k, {}).get("unit", ""), "total": v}
         for k, v in restocked.items()],
        key=lambda x: x["total"], reverse=True
    )[:10]

    return jsonify({
        "total_value": round(total_value, 2),
        "total_items": total_items,
        "low_stock_count": len(low_stock),
        "low_stock": low_stock,
        "top_consumed": top_consumed,
        "top_restocked": top_restocked,
        "total_usage_events": len(usage),
    })


# ---------------------------------------------------------------------------
# Warehouse catalogue (authoritative list used by UI + seeds)
# ---------------------------------------------------------------------------

WAREHOUSES = {
    "Cheney Brothers": [
        "Riviera Beach, FL",
        "Ocala, FL",
        "Punta Gorda, FL",
    ],
    "US Foods": [
        "Manassas, VA",
        "Zebulon, NC",
        "La Mirada, CA",
        "Chicago, IL",
        "Alcoa, TN",
    ],
}


@app.route("/api/warehouses")
def api_warehouses():
    return jsonify(WAREHOUSES)


# ---------------------------------------------------------------------------
# API – Distributors (unified view across Cheney Brothers and US Foods)
# ---------------------------------------------------------------------------

@app.route("/api/distributors")
def api_distributors():
    inv = load_inventory()
    items_enriched = [_enrich_on_order(dict(v)) for v in inv.values()]
    groups: dict[str, list] = {}
    for item in items_enriched:
        dist = item.get("distributor") or "Unassigned"
        groups.setdefault(dist, []).append(item)

    summary = []
    for dist, items in sorted(groups.items()):
        total_qty = sum(i["quantity"] for i in items)
        total_value = sum(i["quantity"] * i["price"] for i in items)
        total_on_order = sum(i.get("on_order_qty", 0) for i in items)
        low = [i for i in items if i["quantity"] <= i["low_stock_threshold"]]

        # Sub-group by warehouse
        wh_groups: dict[str, list] = {}
        for i in items:
            wh_groups.setdefault(i.get("warehouse") or "Unassigned", []).append(i)
        warehouses = []
        for wh_name, wh_items in sorted(wh_groups.items()):
            warehouses.append({
                "warehouse": wh_name,
                "item_count": len(wh_items),
                "total_quantity": round(sum(x["quantity"] for x in wh_items), 2),
                "total_value": round(sum(x["quantity"] * x["price"] for x in wh_items), 2),
                "total_on_order": round(sum(x.get("on_order_qty", 0) for x in wh_items), 2),
                "low_stock_count": sum(1 for x in wh_items if x["quantity"] <= x["low_stock_threshold"]),
                "items": sorted(wh_items, key=lambda x: x["name"]),
            })

        summary.append({
            "distributor": dist,
            "item_count": len(items),
            "total_quantity": round(total_qty, 2),
            "total_value": round(total_value, 2),
            "total_on_order": round(total_on_order, 2),
            "low_stock_count": len(low),
            "warehouses": warehouses,
        })
    return jsonify(summary)


# ---------------------------------------------------------------------------
# API – Sync (pull current on-hand from distributors)
# ---------------------------------------------------------------------------

@app.route("/api/sync", methods=["POST"])
def api_sync():
    from sync_inventory import sync_all

    dry_run = bool((request.json or {}).get("dry_run", False))
    reports = sync_all(dry_run=dry_run)
    return jsonify({"dry_run": dry_run, "reports": reports})


@app.route("/api/seed", methods=["POST"])
def api_seed():
    from seed_bagels import seed

    reset = bool((request.json or {}).get("reset", False))
    try:
        summary = seed(reset=reset)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 200
    return jsonify({"ok": True, **summary})


@app.route("/api/migrate-units", methods=["POST"])
def api_migrate_units():
    from inventory_tracker import migrate_units_to_case

    inv = load_inventory()
    try:
        summary = migrate_units_to_case(inv)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 200
    save_inventory(inv)
    return jsonify({"ok": True, **summary})


@app.route("/api/email/scan", methods=["POST"])
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
            # Mirror cowork_graph_scan: pre-filter to attachment-bearing
            # messages so the page budget isn't burned on non-PO mail.
            # Graph rejects hasAttachments + $orderby (InefficientFilter),
            # so _scan_ms365_mailbox drops orderby when the filter
            # contains "hasAttachments".
            filter_override = f"hasAttachments eq true and receivedDateTime ge {iso}"

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
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": _tb.format_exc()[-2000:],
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
    except Exception as exc:  # noqa: BLE001
        report = {
            "distributor": "Email Inbox",
            "source": "unknown",
            "status": "error",
            "fetched": 0,
            "updated": 0,
            "unchanged": 0,
            "unmatched": [],
            "changes": [],
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": _tb.format_exc()[-2000:],
            "messages_seen": 0,
            "messages_parsed": 0,
        }
    return jsonify({"dry_run": dry_run, "reports": [report]})


@app.route("/api/email/ingest-events", methods=["POST"])
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
              "po_revision": "1"           # numeric string; "" allowed
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
                "error": f"import failed: {type(exc).__name__}: {exc}",
                "traceback": _tb.format_exc()[-2000:],
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

    built: list[EmailEvent] = []
    build_errors: list[str] = []
    for idx, e in enumerate(raw_events):
        if not isinstance(e, dict):
            build_errors.append(f"events[{idx}]: not an object")
            continue
        try:
            etype = e.get("event_type")
            if etype not in ("on_hand", "restock", "usage"):
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
            ))
        except (TypeError, ValueError, KeyError) as exc:
            build_errors.append(f"events[{idx}]: {exc}")

    try:
        report = _apply_events(
            events=built,
            messages_seen=messages_seen,
            messages_parsed=messages_parsed,
            errors=errors + build_errors,
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
                "error": str(exc),
                "messages_seen": messages_seen,
                "messages_parsed": messages_parsed,
            }],
        }), 500

    return jsonify({"dry_run": dry_run, "reports": [report]})


@app.route("/api/export.xlsx")
def api_export_xlsx():
    from openpyxl import Workbook
    from export_bagels_xlsx import _write_summary_sheet, _write_items_sheet

    inv = load_inventory()
    items = list(inv.values())
    cheney = [i for i in items if (i.get("distributor") or "") == "Cheney Brothers"]
    usfoods = [i for i in items if (i.get("distributor") or "") == "US Foods"]

    wb = Workbook()
    _write_summary_sheet(wb.active, inv)
    wb.active.title = "Summary"
    _write_items_sheet(wb.create_sheet("Unified List"), items)
    _write_items_sheet(wb.create_sheet("Cheney Brothers"), cheney)
    _write_items_sheet(wb.create_sheet("US Foods"), usfoods)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="bagel_inventory.xlsx",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
