"""Admin / maintenance blueprint — PO admin ops (set order-date, remove PO,
uncancel PO) and the sync / seed / migrate-units maintenance endpoints.
Extracted from app.py (refactor — see REFACTOR_PLAN.md)."""
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from core.errors import _safe_err
from core.util import with_data_lock
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
            _raw = str(order_raw).strip()
            if _raw.endswith(("Z", "z")):
                _raw = _raw[:-1] + "+00:00"
            order_dt = datetime.fromisoformat(_raw)
        except ValueError:
            return jsonify({
                "ok": False,
                "error": "order_date must be ISO 8601 (YYYY-MM-DD or full datetime)",
            }), 400
        if order_dt.tzinfo is not None:
            # A tz-aware value stored here broke every later load_inventory().
            order_dt = order_dt.astimezone().replace(tzinfo=None)
        order_iso = order_dt.isoformat()

    from inventory_tracker import data_lock
    with data_lock():
        inv = load_inventory()
        updated = 0
        touched_items: list[str] = []
        for key, item in inv.items():
            pending = item.get("on_order") or []
            for entry in pending:
                if (entry.get("po_number") or "") != po_number:
                    continue
                entry["ordered_at"] = order_iso
                # Mark the provenance so the legacy subject-date rebase that
                # runs on every read leaves this entry alone.
                entry["ordered_at_source"] = "operator"
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
@with_data_lock
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


@admin_bp.route("/api/admin/restore-absorbed-po", methods=["POST"])
@with_data_lock
def api_admin_restore_absorbed_po():
    """Put a delivered PO back into on-hand when the count that "absorbed" it
    never actually contained it.

    The rollover marks a PO absorbed when a count is dated on/after its
    arrival day. A distributor's on-hand screen can lag its own receiving by
    hours: Houston 393072B2 was delivered 10/19 08:00 CT (Lineage-confirmed),
    Tom Foley's 11:20 CT buyer screen still listed all 1,120 cs under Order
    with Hand totalling 404 cs, and the PO was zeroed as "already on hand".

    Body: {po_number, count_at, reason, dry_run=true, force=false}
      count_at -- the exact last_count_at of the count that missed the PO.
                  Each line's SKU must still carry it: if the warehouse has
                  counted again since, that newer count governs and the line
                  is left alone.
    Each live on_order_absorbed row of the PO is converted IN PLACE into the
    on_order_rollover receipt it should have been (amount = -cases,
    not_in_count_at = count_at) and the cases are added to quantity. Later
    re-reads of that count keep the receipt on top (_receipts_after_count);
    the next count absorbs it normally. Refused unless the counted figures
    prove the count could not contain the delivery
    (core.util.count_missed_delivery), or force=true.
    """
    from core.util import (absorbed_row_live, absorbed_row_qty,
                           count_missed_delivery, naive_local_dt)
    from inventory_tracker import (load_usage, save_usage, load_po_delivery,
                                   append_inventory_audit)
    from integrations.po_delivery import norm_po
    body = request.json or {}
    po_number = (body.get("po_number") or "").strip()
    count_at = (body.get("count_at") or "").strip()
    reason = (body.get("reason") or "").strip()
    dry_run = body.get("dry_run", True) is not False
    force = bool(body.get("force"))
    if not po_number or not count_at:
        return jsonify({"ok": False, "error": "po_number and count_at required"}), 400
    if not dry_run and not reason:
        return jsonify({"ok": False, "error": "reason required for a live run"}), 400

    inv = load_inventory()
    usage = load_usage()
    try:
        trk = (load_po_delivery() or {}).get(norm_po(po_number)) or {}
    except Exception:  # noqa: BLE001
        trk = {}
    confirmed = (trk.get("status") == "delivered")

    rows, skipped = [], []
    for idx, e in enumerate(usage):
        if (e.get("po_number") or "") != po_number or not absorbed_row_live(e):
            continue
        key = e.get("item_key") or ""
        item = inv.get(key)
        if item is None:
            skipped.append(f"{key}: SKU not in inventory")
            continue
        if (item.get("last_count_at") or "") != count_at:
            skipped.append(f"{item.get('name', key)}: last count is "
                           f"{item.get('last_count_at') or 'none'}, not {count_at} "
                           f"- the newer count governs")
            continue
        rows.append((idx, key, item, absorbed_row_qty(e)))
    if not rows:
        return jsonify({"ok": False, "po_number": po_number,
                        "error": "no live absorbed lines for this PO on that count",
                        "skipped": skipped}), 404

    arrival = naive_local_dt(usage[rows[0][0]].get("arrival_date"))
    cdt = naive_local_dt(count_at)
    hrs = ((cdt - arrival).total_seconds() / 3600.0) if (arrival and cdt) else 0.0
    by_key: dict = {}
    for _idx, key, item, qty in rows:
        line = by_key.setdefault(key, {"receipt": 0.0,
                                       "counted": float(item.get("last_count_qty") or 0),
                                       "weekly_usage": float(item.get("weekly_usage") or 0)})
        line["receipt"] += qty
    missed, why = count_missed_delivery(list(by_key.values()), hrs)
    if arrival and cdt and cdt < arrival:
        missed, why = True, "count stamped before the delivery time"
    if not missed and not force:
        return jsonify({"ok": False, "po_number": po_number,
                        "error": "the counted figures could contain this delivery; "
                                 "pass force=true only if you are sure they do not",
                        "lines": by_key, "hours_after_delivery": round(hrs, 2)}), 409

    now = datetime.now().isoformat()
    lines, audit = [], []
    for idx, key, item, qty in rows:
        old_q = float(item.get("quantity") or 0)
        lines.append({"name": item.get("name", key), "qty": qty,
                      "counted": item.get("last_count_qty"),
                      "old_quantity": round(old_q, 4),
                      "new_quantity": round(old_q + qty, 4)})
        if dry_run:
            continue
        row = usage[idx]
        row["absorbed_note"] = row.get("note") or ""
        row["was_absorbed_qty"] = qty
        row.pop("absorbed_qty", None)
        row["source"] = "on_order_rollover"
        row["amount"] = -qty
        row["not_in_count_at"] = count_at
        row["count_missed_reason"] = why or "operator: count did not contain it"
        row["restored_at"] = now
        row["restored_reason"] = reason
        if confirmed:
            row["delivery_confirmed"] = True
        row["note"] = (f"PO {po_number} arrived ({(row.get('arrival_date') or '')[:16]})"
                       f" - not in the {count_at} count; restored {now[:10]}: {reason}")
        item["quantity"] = old_q + qty
        item["updated"] = now
        audit.append({"ts": now, "source": "admin-restore-absorbed-po",
                      "name": item.get("name", key),
                      "warehouse": item.get("warehouse", ""),
                      "event_type": "restock", "old_quantity": old_q,
                      "new_quantity": old_q + qty, "delta": qty,
                      "po_number": po_number})
    if not dry_run:
        save_inventory(inv)
        save_usage(usage)
        try:
            append_inventory_audit(audit)
        except Exception:  # noqa: BLE001 -- audit is best-effort
            pass
    return jsonify({"ok": True, "dry_run": dry_run, "po_number": po_number,
                    "count_at": count_at, "delivery_confirmed": confirmed,
                    "hours_after_delivery": round(hrs, 2),
                    "count_missed": missed, "why": why, "forced": bool(force and not missed),
                    "restored_cs": round(sum(l["qty"] for l in lines), 2),
                    "lines": lines, "skipped": skipped})


@admin_bp.route("/api/admin/uncancel-po", methods=["POST"])
@with_data_lock
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


# Fields /api/inventory adds on the way out (see blueprints/inventory.py
# _enrich_on_order). A snapshot taken from that endpoint carries them, and
# they must not be written back into the stored item.
_DERIVED_FIELDS = ("on_order_qty", "on_order_next_eta",
                   "on_order_next_arrival", "on_order_next_is_actual")


@admin_bp.route("/api/admin/restore-inventory", methods=["POST"])
@with_data_lock
def api_admin_restore_inventory():
    """Restore inventory from a snapshot taken off /api/inventory.

    Exists because on 2026-09-17 inventory.json was blanked and there was no
    way back in short of re-seeding and losing every count. Accepts the list
    that /api/inventory returns, strips the derived fields, and rebuilds the
    stored dict keyed on lowercased name.

    Body:
      items      list of item dicts (required, non-empty)
      dry_run    bool -- report what would change, write nothing
      mode       "replace"     overwrite the whole file (default)
                 "fill-missing" only add SKUs not currently present, leaving
                                existing ones untouched
    """
    body = request.json or {}
    items = body.get("items")
    dry_run = bool(body.get("dry_run", False))
    mode = (body.get("mode") or "replace").strip()
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False,
                        "error": "items must be a non-empty list"}), 400
    if mode not in ("replace", "fill-missing"):
        return jsonify({"ok": False, "error": "unknown mode %r" % mode}), 400

    incoming = {}
    for raw in items:
        if not isinstance(raw, dict) or not raw.get("name"):
            return jsonify({"ok": False,
                            "error": "every item needs a name"}), 400
        item = {k: v for k, v in raw.items() if k not in _DERIVED_FIELDS}
        incoming[str(raw["name"]).lower()] = item

    try:
        current = load_inventory()
    except Exception:  # noqa: BLE001 -- unreadable is exactly when this runs
        current = {}

    added = sorted(set(incoming) - set(current))
    kept = sorted(set(incoming) & set(current))
    dropped = sorted(set(current) - set(incoming))
    if mode == "fill-missing":
        result = dict(current)
        for k in added:
            result[k] = incoming[k]
        dropped = []
    else:
        result = incoming

    summary = {
        "ok": True, "dry_run": dry_run, "mode": mode,
        "before_sku_count": len(current), "after_sku_count": len(result),
        "added": len(added), "overwritten": len(kept) if mode == "replace" else 0,
        "dropped": len(dropped), "dropped_keys": dropped[:20],
        "on_order_rows": sum(len(v.get("on_order") or [])
                             for v in result.values()),
        "total_quantity": round(sum(float(v.get("quantity") or 0)
                                    for v in result.values()), 2),
    }
    if not dry_run:
        save_inventory(result)
    return jsonify(summary)


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
