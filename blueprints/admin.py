"""Admin / maintenance blueprint — PO admin ops (set order-date, remove PO,
uncancel PO) and the sync / seed / migrate-units maintenance endpoints.
Extracted from app.py (refactor — see REFACTOR_PLAN.md)."""
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from core.errors import _safe_err
from inventory_tracker import load_inventory, save_inventory

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/api/admin/po-order-date", methods=["POST"])
def api_admin_po_order_date():
    """Set (or clear) ``ordered_at`` on every on_order entry matching a
    PO number. Use when a PO was ingested before po_order_date was
    plumbed through the scanner — the on_order entry inherited the
    ingestion timestamp instead of the PDF's printed "ORDER DATE",
    which threw off the Pending POs view and the 30-day rollover ETA.

    Body:
      po_number       required.
      order_date      ISO date (YYYY-MM-DD) or empty/null to clear.
      recompute_eta   bool; default true. If true and order_date is
                      set, also reset eta = order_date + lead_days
                      (entry-by-entry, using each entry's lead_days).

    Returns {ok, po_number, order_date, entries_updated, items}.
    """
    from datetime import datetime, timedelta
    from inventory_tracker import load_inventory, save_inventory

    body = request.json or {}
    po_number = (body.get("po_number") or "").strip()
    if not po_number:
        return jsonify({"ok": False, "error": "po_number required"}), 400
    order_raw = body.get("order_date")
    recompute_eta = bool(body.get("recompute_eta", True))

    if order_raw is None or (isinstance(order_raw, str)
                             and not order_raw.strip()):
        order_iso = ""
        order_dt = None
    else:
        try:
            order_dt = datetime.fromisoformat(str(order_raw).strip())
        except ValueError:
            return jsonify({
                "ok": False,
                "error": "order_date must be ISO 8601 (YYYY-MM-DD or full datetime)",
            }), 400
        order_iso = order_dt.isoformat()

    inv = load_inventory()
    updated = 0
    touched_items: list[str] = []
    for key, item in inv.items():
        pending = item.get("on_order") or []
        for entry in pending:
            if (entry.get("po_number") or "") != po_number:
                continue
            entry["ordered_at"] = order_iso
            if recompute_eta and order_dt is not None:
                lead = int(entry.get("lead_days") or 0)
                if lead > 0:
                    entry["eta"] = (order_dt + timedelta(days=lead)).isoformat()
            updated += 1
            name = item.get("name") or key
            if name not in touched_items:
                touched_items.append(name)
    save_inventory(inv)
    return jsonify({
        "ok": True,
        "po_number": po_number,
        "order_date": order_iso,
        "entries_updated": updated,
        "items": touched_items,
    })


@admin_bp.route("/api/admin/remove-po", methods=["POST"])
def api_admin_remove_po():
    """Drop all pending on_order entries matching a po_number.

    Used to manually retire a PO whose source email has been deleted or
    archived (so the auto-supersede via re-scan can't reach it). The
    operation does NOT touch already-rolled-over quantity or usage
    entries — only items still sitting in item["on_order"].
    """
    body = request.json or {}
    po_number = (body.get("po_number") or "").strip()
    reason = (body.get("reason") or "canceled by distributor").strip()
    if not po_number:
        return jsonify({"ok": False, "error": "po_number required"}), 400
    from inventory_tracker import (
        load_inventory, save_inventory,
        load_canceled_pos, save_canceled_pos,
    )
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
    # Record the PO in the ignore list so the email scanner won't
    # re-ingest it from a still-sitting source email.
    canceled = load_canceled_pos()
    canceled[po_number] = {
        "canceled_at": datetime.now().isoformat(timespec="seconds"),
        "reason":      reason,
        "removed_entries": removed,
        "affected_items":  affected,
    }
    save_canceled_pos(canceled)
    return jsonify({
        "ok": True,
        "po_number": po_number,
        "removed_entries": removed,
        "affected_items": affected,
        "added_to_ignore_list": True,
    })


@admin_bp.route("/api/admin/uncancel-po", methods=["POST"])
def api_admin_uncancel_po():
    """Remove a po_number from the canceled-POs ignore list.

    Inverse of /api/admin/remove-po. Use when a PO was canceled in error
    (or when an operator needs to allow a re-ingest of a PO whose stored
    entries were wiped). Does NOT restore the previously-removed
    on_order entries -- those have to come back via a fresh ingest from
    the source email or a manual POST to /api/email/ingest-events.
    """
    body = request.json or {}
    po_number = (body.get("po_number") or "").strip()
    if not po_number:
        return jsonify({"ok": False, "error": "po_number required"}), 400
    from inventory_tracker import load_canceled_pos, save_canceled_pos
    canceled = load_canceled_pos()
    prior = canceled.pop(po_number, None)
    save_canceled_pos(canceled)
    return jsonify({
        "ok": True,
        "po_number": po_number,
        "was_canceled": prior is not None,
        "prior_entry": prior,
    })


@admin_bp.route("/api/sync", methods=["POST"])
def api_sync():
    from sync_inventory import sync_all

    dry_run = bool((request.json or {}).get("dry_run", False))
    reports = sync_all(dry_run=dry_run)
    return jsonify({"dry_run": dry_run, "reports": reports})


@admin_bp.route("/api/seed", methods=["POST"])
def api_seed():
    from seed_bagels import seed

    reset = bool((request.json or {}).get("reset", False))
    try:
        summary = seed(reset=reset)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc)}), 200
    return jsonify({"ok": True, **summary})


@admin_bp.route("/api/migrate-units", methods=["POST"])
def api_migrate_units():
    from inventory_tracker import migrate_units_to_case

    inv = load_inventory()
    try:
        summary = migrate_units_to_case(inv)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc)}), 200
    save_inventory(inv)
    return jsonify({"ok": True, **summary})
