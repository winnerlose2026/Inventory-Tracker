"""Action log: every change request that reaches the app, who sent it, and
what came back.

The data files already record WHAT moved -- inventory_audit.json (each count,
PO booking and replacement a scan applied) and usage.json (arrivals, burns,
reversals). What they never recorded is the REQUEST that caused a change:
a status set on the Pending POs tab, a ship date typed in, an arrived PO
edited, a mailbox scan run by the cron, a Lineage follow-up sent. Several of
those move nothing in the files at all (a status label, a date on an arrived
row), which is how La Mirada 7135734C sat "In Production" on one tab and
"arrived" on the other with no trace of who did what or when.

app.py's after_request hook calls ``record_request`` for every POST / PUT /
DELETE under /api/. Entries are one JSON object per line in
``<DATA_DIR>/activity_log.jsonl`` -- append-only, so concurrent workers never
rewrite each other, trimmed to the newest ``KEEP_LINES`` once the file passes
``TRIM_BYTES``. Best-effort throughout: logging must never fail a request.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime

KEEP_LINES = 20000
TRIM_BYTES = 8 * 1024 * 1024

# Endpoint -> what the operator would call it.
LABELS = {
    "pos.api_pending_set_status": "PO status set",
    "pos.api_on_order_ship_date": "Ship date set",
    "pos.api_pending_reopen": "PO reopened",
    "pos.api_arrived_po_adjust": "Arrived PO edited",
    "pos.api_chefs_warehouse_ship_date": "CW ship date set",
    "pos.api_chefs_warehouse_cancel": "CW PO canceled",
    "pos.api_chefs_warehouse_ingest_pos": "CW POs ingested",
    "pos.api_pos_reconcile": "PO reconcile check",
    "admin.api_admin_remove_po": "PO canceled",
    "admin.api_admin_po_order_date": "PO order date set",
    "admin.api_admin_restore_absorbed_po": "Absorbed PO restored to on-hand",
    "admin.api_admin_absorb_po_into_count": "PO absorbed into count",
    "admin.api_admin_uncancel_po": "PO un-canceled",
    "admin.api_admin_restore_inventory": "Inventory restored from backup",
    "admin.api_sync": "Distributor sync",
    "admin.api_seed": "Inventory seeded",
    "admin.api_migrate_units": "Units migrated",
    "email.api_email_scan": "Mailbox scan",
    "email.api_email_ingest_events": "Events ingested",
    "email.api_email_send": "Email sent",
    "email.api_ingest_cheney_inventory_csv": "Cheney inventory CSV ingested",
    "freight.api_freight_ingest": "Freight invoices ingested",
    "freight.api_freight_scan": "Freight scan",
    "freight.api_freight_apply_ship_dates": "Ship dates applied from freight",
    "freight.api_freight_lead_times_apply": "Lead times applied",
    "inventory.api_add": "SKU added",
    "inventory.api_update": "SKU edited",
    "inventory.api_remove": "SKU removed",
    "inventory.api_use": "Manual use",
    "inventory.api_restock": "Manual restock",
    "inventory.api_usage_reverse": "Usage entry reversed",
    "delivery.api_delivery_sync_threads": "Lineage threads synced",
    "delivery.api_delivery_events": "Delivery events posted",
    "delivery.api_delivery_chased": "Lineage chase recorded",
    "delivery.api_delivery_send_followups": "Lineage follow-ups sent",
    "production.api_production_ingest": "Production ingested",
    "production.api_production_scan": "Production scan",
    "reporting.api_forecast_decrement_daily": "Daily forecast burn",
    "reporting.api_forecast_true_up": "Forecast true-up",
    "reporting.api_forecast_backfill_historical": "Forecast backfill",
    "reporting.api_sales_ingest": "Sales ingested",
    "reporting.api_admin_labor_ingest": "Labor ingested",
    "reporting.api_admin_bakery_sales_ingest": "Bakery sales ingested",
    "production.api_admin_production_reclassify": "Production reclassified",
    "production.api_admin_production_renormalize_varieties": "Production varieties renormalized",
    "production.api_admin_production_repair": "Production repaired",
    "production.api_production_delete": "Production record deleted",
    "webhooks.api_graph_subscriptions": "Graph subscription created",
    "webhooks.api_graph_subscriptions_renew": "Graph subscriptions renewed",
}

# Request keys worth keeping (values truncated); list values become counts.
_REQ_KEYS = ("po_number", "status", "ship_date", "arrival_date", "item_key",
             "name", "amount", "qty", "reason", "source", "dry_run",
             "lookback_days", "count_at", "force", "override_freight",
             "warehouse", "distributor", "note", "quantity")
_REQ_LIST_KEYS = ("lines", "events", "cw_pos", "items", "to")
# Response keys that say what happened.
_RESP_KEYS = ("ok", "error", "status", "restored_lines", "removed_cs",
              "onhand_delta_cs", "unrolled", "lines_touched", "entries_updated",
              "removed_entries", "arrival_stamped_lines", "added", "updated",
              "unchanged", "applied", "skipped", "sent", "messages_seen",
              "messages_parsed", "dry_run", "delivery_reopened", "count")
_REPORT_KEYS = ("messages_seen", "messages_parsed", "updated", "unchanged",
                "added", "applied", "restock", "on_hand", "errors", "error_count",
                "status", "stamped", "skipped")


def _log_path():
    import inventory_tracker as it          # DATA_DIR is patched in tests
    return it.DATA_DIR / "activity_log.jsonl"


@contextmanager
def _flock(path):
    try:
        import fcntl
    except ImportError:                      # Windows desktop GUI
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(str(path) + ".lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def _short(v, n=160):
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize_request(body) -> dict:
    if not isinstance(body, dict):
        return {}
    out = {}
    for k in _REQ_KEYS:
        if k in body and body[k] not in ("", None):
            out[k] = _short(body[k])
    for k in _REQ_LIST_KEYS:
        if isinstance(body.get(k), list):
            out[k] = len(body[k])
    return out


def summarize_response(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    out = {}
    for k in _RESP_KEYS:
        v = payload.get(k)
        if v is None or isinstance(v, (dict, list)):
            continue
        out[k] = _short(v)
    rep = payload.get("report")
    if isinstance(rep, dict):
        for k in _REPORT_KEYS:
            v = rep.get(k)
            if isinstance(v, list):
                out[f"report_{k}"] = len(v)
            elif v is not None and not isinstance(v, dict):
                out[f"report_{k}"] = _short(v)
        by = rep.get("by_event_type")
        if isinstance(by, dict):
            for k, v in list(by.items())[:6]:
                if isinstance(v, (int, float)):
                    out[f"events_{k}"] = v
    mb = payload.get("moved_back_on_order")
    if isinstance(mb, dict):
        out["moved_back_cs"] = mb.get("cs")
        out["onhand_removed_cs"] = mb.get("onhand_removed_cs")
    return out


def record(entry: dict) -> None:
    """Append one entry. Never raises."""
    try:
        path = _log_path()
        line = json.dumps(entry, default=str, separators=(",", ":"))
        with _flock(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            try:
                if path.stat().st_size > TRIM_BYTES:
                    with open(path, encoding="utf-8") as f:
                        keep = f.readlines()[-KEEP_LINES:]
                    tmp = path.with_name(path.name + ".trim")
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.writelines(keep)
                    os.replace(tmp, path)
            except OSError:
                pass
    except Exception:  # noqa: BLE001 -- logging must never fail a request
        pass


def record_request(*, endpoint: str, method: str, path: str, status: int,
                   actor: str, body, payload) -> None:
    req = summarize_request(body)
    if req.get("dry_run") is True:
        return                               # a preview changed nothing
    record({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "endpoint": endpoint or "",
        "label": LABELS.get(endpoint or "", path),
        "method": method,
        "path": path,
        "status": int(status),
        "actor": actor,
        "po_number": str(req.get("po_number") or ""),
        "request": req,
        "result": summarize_response(payload),
    })


def load(limit: int = 5000, since: str = "") -> list:
    """Newest-first entries (optionally only those at/after ``since``)."""
    path = _log_path()
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if since and (e.get("ts") or "") < since:
            break
        out.append(e)
        if limit and len(out) >= limit:
            break
    return out
