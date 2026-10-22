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
    dry_run = bool(body.get("dry_run", False))
    if not po_number:
        return jsonify({"ok": False, "error": "po_number required"}), 400
    from core.util import _norm_po_key
    from inventory_tracker import (
        load_inventory, save_inventory,
        load_canceled_pos, save_canceled_pos, append_inventory_audit,
    )
    # Match the way sync_inventory._apply_events matches the canceled list
    # (normalised). Exact matching here removed nothing for "hhb-7500000o"
    # while the scanner, which normalises, then ignored every later revision
    # of 7500000O -- the stale booking stayed pending and rolled in.
    key_norm = _norm_po_key(po_number)
    inv = load_inventory()
    removed = 0
    affected = []
    removed_rows = []           # kept so /api/admin/uncancel-po can restore them
    stored_po = ""
    now_iso = datetime.now().isoformat(timespec="seconds")
    audit = []
    for key, item in inv.items():
        pending = item.get("on_order") or []
        kept = [e for e in pending
                if _norm_po_key(e.get("po_number") or "") != key_norm]
        if len(kept) != len(pending):
            gone = [e for e in pending
                    if _norm_po_key(e.get("po_number") or "") == key_norm]
            removed += len(gone)
            affected.append(item.get("name", key))
            stored_po = stored_po or (gone[0].get("po_number") or "")
            removed_rows.extend({"item_key": key, "entry": dict(e)} for e in gone)
            audit.append({"ts": now_iso, "source": "admin-remove-po",
                          "name": item.get("name", key),
                          "warehouse": item.get("warehouse", ""),
                          "event_type": "po_removed",
                          "old_quantity": item.get("quantity"),
                          "new_quantity": item.get("quantity"), "delta": 0,
                          "po_number": stored_po or po_number,
                          "on_order_removed_cs": round(sum(
                              float(e.get("qty") or 0) for e in gone), 2),
                          "reason": reason})
        if not dry_run:
            item["on_order"] = kept
    # Key the ignore list on the PO number as booked, so the ledger's exact
    # lookup and the scanner's normalised one both find it.
    canon = stored_po or key_norm
    if not dry_run:
        save_inventory(inv)
        # Record the PO in the ignore list so the email scanner won't
        # re-ingest it from a still-sitting source email.
        canceled = load_canceled_pos()
        canceled[canon] = {
            "canceled_at": now_iso,
            "reason":      reason,
            "removed_entries": removed,
            "affected_items":  affected,
            "removed_rows":    removed_rows,
        }
        save_canceled_pos(canceled)
        try:
            append_inventory_audit(audit)
        except Exception:  # noqa: BLE001 -- audit is best-effort
            pass
    return jsonify({
        "ok": True,
        "dry_run": dry_run,
        "po_number": canon,
        "removed_entries": removed,
        "removed_cs": round(sum(float(r["entry"].get("qty") or 0)
                                for r in removed_rows), 2),
        "affected_items": affected,
        "added_to_ignore_list": not dry_run,
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


@admin_bp.route("/api/admin/absorb-po-into-count", methods=["POST"])
@with_data_lock
def api_admin_absorb_po_into_count():
    """The inverse of restore-absorbed-po: a PO rolled into on-hand AFTER a
    count that already contained it, so its cases are counted twice.

    Manassas 7199415O: picked up 10/13, Bozel's ETA 1300 on 10/16; never
    tracked by Lineage, so it rolled on the flat 7-day rule on 10/20 -- after
    the 10/19 count, which already showed it (Plain 13 -> 20 and Everything
    21 -> 54 in a week with 9 and 12 cs/wk burn). On-hand carried 112 cs twice.

    Body: {po_number, count_at, arrived_on (YYYY-MM-DD, on/before the count),
           reason, dry_run=true, force=false}
    Each live on_order_rollover row of the PO whose SKU still carries
    last_count_at == count_at is converted IN PLACE into the
    on_order_absorbed row it should have been (amount 0, absorbed_qty =
    cases, arrival_date = arrived_on) and the cases come off quantity.
    Refused when the counted figures prove the count could NOT contain the
    delivery (core.util.count_missed_delivery), unless force=true.
    """
    from core.util import count_missed_delivery, naive_local_dt, rollover_row_live
    from inventory_tracker import load_usage, save_usage, append_inventory_audit
    body = request.json or {}
    po_number = (body.get("po_number") or "").strip()
    count_at = (body.get("count_at") or "").strip()
    arrived_on = (body.get("arrived_on") or "").strip()[:10]
    reason = (body.get("reason") or "").strip()
    dry_run = body.get("dry_run", True) is not False
    force = bool(body.get("force"))
    if not po_number or not count_at or not arrived_on:
        return jsonify({"ok": False, "error": "po_number, count_at and arrived_on required"}), 400
    if arrived_on > count_at[:10]:
        return jsonify({"ok": False, "error": "arrived_on is after the count -- "
                        "the count could not contain it"}), 400
    if not dry_run and not reason:
        return jsonify({"ok": False, "error": "reason required for a live run"}), 400

    inv = load_inventory()
    usage = load_usage()
    rows, skipped = [], []
    for idx, e in enumerate(usage):
        if ((e.get("po_number") or "") != po_number
                or e.get("source") != "on_order_rollover" or not rollover_row_live(e)):
            continue
        key = e.get("item_key") or ""
        item = inv.get(key)
        if item is None:
            skipped.append(f"{key}: SKU not in inventory")
            continue
        if (item.get("last_count_at") or "") != count_at:
            skipped.append(f"{item.get('name', key)}: last count is "
                           f"{item.get('last_count_at') or 'none'}, not {count_at}")
            continue
        rows.append((idx, key, item, abs(float(e.get("amount") or 0))))
    if not rows:
        return jsonify({"ok": False, "po_number": po_number,
                        "error": "no live rollover lines for this PO on that count",
                        "skipped": skipped}), 404

    adt = naive_local_dt(arrived_on + "T12:00:00")
    cdt = naive_local_dt(count_at)
    hrs = ((cdt - adt).total_seconds() / 3600.0) if (adt and cdt) else 0.0
    by_key: dict = {}
    for _idx, key, item, qty in rows:
        line = by_key.setdefault(key, {"receipt": 0.0,
                                       "counted": float(item.get("last_count_qty") or 0),
                                       "weekly_usage": float(item.get("weekly_usage") or 0)})
        line["receipt"] += qty
    missed, why = count_missed_delivery(list(by_key.values()), hrs)
    if missed and not force:
        return jsonify({"ok": False, "po_number": po_number,
                        "error": f"the counted figures can't contain this delivery ({why})",
                        "lines": by_key}), 409

    now = datetime.now().isoformat()
    lines, audit = [], []
    for idx, key, item, qty in rows:
        old_q = float(item.get("quantity") or 0)
        lines.append({"name": item.get("name", key), "qty": qty,
                      "counted": item.get("last_count_qty"),
                      "old_quantity": round(old_q, 4),
                      "new_quantity": round(old_q - qty, 4)})
        if dry_run:
            continue
        row = usage[idx]
        row["rollover_note"] = row.get("note") or ""
        row["rolled_arrival_date"] = row.get("arrival_date") or ""
        row["source"] = "on_order_absorbed"
        row["amount"] = 0
        row["absorbed_qty"] = qty
        row["arrival_date"] = arrived_on + "T00:00:00"
        row["absorbed_into_count_at"] = count_at
        row["absorbed_at"] = now
        row["absorbed_reason"] = reason
        row["note"] = (f"PO {po_number} arrived {arrived_on} - absorbed by the "
                       f"{count_at[:10]} count - {qty:g} cs already on hand, not "
                       f"re-added (operator {now[:10]}: {reason})")
        item["quantity"] = old_q - qty
        item["updated"] = now
        audit.append({"ts": now, "source": "admin-absorb-po-into-count",
                      "name": item.get("name", key),
                      "warehouse": item.get("warehouse", ""),
                      "event_type": "correction", "old_quantity": old_q,
                      "new_quantity": old_q - qty, "delta": -qty,
                      "po_number": po_number})
    if not dry_run:
        save_inventory(inv)
        save_usage(usage)
        try:
            append_inventory_audit(audit)
        except Exception:  # noqa: BLE001 -- audit is best-effort
            pass
    return jsonify({"ok": True, "dry_run": dry_run, "po_number": po_number,
                    "count_at": count_at, "arrived_on": arrived_on,
                    "hours_before_count": round(hrs, 2), "forced": bool(force and missed),
                    "absorbed_cs": round(sum(l["qty"] for l in lines), 2),
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
    from core.util import _norm_po_key
    from inventory_tracker import (load_canceled_pos, save_canceled_pos,
                                   load_inventory, save_inventory,
                                   append_inventory_audit)
    canceled = load_canceled_pos()
    prior = canceled.pop(po_number, None)
    if prior is None:           # same normalised matching as remove-po
        for k in [k for k in canceled if _norm_po_key(k) == _norm_po_key(po_number)]:
            prior = canceled.pop(k)
            break
    # Put back the pending rows remove-po took out (recorded since 10-22), so
    # an accidental cancel is one click to undo instead of a PO that is gone
    # once its email ages out of the scan's 14-day lookback. A SKU that has
    # this PO on order again (re-ingested meanwhile) is left as it is.
    restored, now_iso, audit = 0, datetime.now().isoformat(timespec="seconds"), []
    rows = (prior or {}).get("removed_rows") or []
    if rows:
        inv = load_inventory()
        _pk = _norm_po_key(po_number)
        rebooked = {k for k, it in inv.items()
                    if any(_norm_po_key(p.get("po_number") or "") == _pk
                           for p in (it.get("on_order") or []))}
        for r in rows:
            item = inv.get(r.get("item_key") or "")
            entry = r.get("entry") or {}
            if item is None or (r.get("item_key") or "") in rebooked:
                continue
            item.setdefault("on_order", []).append(dict(entry))
            restored += 1
            audit.append({"ts": now_iso, "source": "admin-uncancel-po",
                          "name": item.get("name", r.get("item_key")),
                          "warehouse": item.get("warehouse", ""),
                          "event_type": "po_restored",
                          "old_quantity": item.get("quantity"),
                          "new_quantity": item.get("quantity"), "delta": 0,
                          "po_number": entry.get("po_number") or po_number,
                          "on_order_restored_cs": float(entry.get("qty") or 0)})
        if restored:
            save_inventory(inv)
    save_canceled_pos(canceled)
    if audit:
        try:
            append_inventory_audit(audit)
        except Exception:  # noqa: BLE001 -- audit is best-effort
            pass
    return jsonify({
        "ok": True,
        "po_number": po_number,
        "was_canceled": prior is not None,
        "prior_entry": prior,
        "restored_entries": restored,
    })


@admin_bp.route("/api/sync", methods=["POST"])
def api_sync():
    from sync_inventory import sync_all

    dry_run = bool((request.json or {}).get("dry_run", False))
    reports = sync_all(dry_run=dry_run)
    return jsonify({"dry_run": dry_run, "reports": reports})


@admin_bp.route("/api/seed", methods=["POST"])
@with_data_lock
def api_seed():
    from seed_bagels import seed

    body = request.json or {}
    reset = bool(body.get("reset", False))
    snapshot = None
    if reset:
        # reset=true replaces every SKU -- counts, on-hand, pending POs. It
        # used to need nothing but the flag, and the per-SKU saves that
        # followed rotated inventory.json.bak away, leaving no local way back.
        if body.get("confirm") != "RESET":
            return jsonify({"ok": False,
                            "error": 'reset wipes every SKU, count and pending '
                                     'PO; resend with "confirm": "RESET"'}), 400
        try:
            snapshot = _snapshot_data_files("pre-seed-reset")
        except Exception as exc:  # noqa: BLE001 -- no snapshot, no reset
            return jsonify({"ok": False,
                            "error": "could not snapshot data before reset: "
                                     + _safe_err(exc)}), 500
    try:
        summary = seed(reset=reset)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc)}), 200
    return jsonify({"ok": True, **summary,
                    **({"snapshot": snapshot} if snapshot else {})})


def _snapshot_data_files(tag: str) -> dict:
    """Copy inventory.json + usage.json to data/backups/<name>-<ts>-<tag>.json
    before a destructive admin action. Returns {file: snapshot path}."""
    import shutil
    import inventory_tracker as it
    bdir = it.DATA_DIR / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    out = {}
    for src in (it.INVENTORY_FILE, it.USAGE_FILE):
        if src.exists():
            dst = bdir / f"{src.stem}-{ts}-{tag}.json"
            shutil.copyfile(src, dst)
            out[src.name] = str(dst)
    return out


# Fields /api/inventory adds on the way out (see blueprints/inventory.py
# _enrich_on_order). A snapshot taken from that endpoint carries them, and
# they must not be written back into the stored item.
_DERIVED_FIELDS = ("on_order_qty", "on_order_next_eta",
                   "on_order_next_arrival", "on_order_next_is_actual",
                   "on_order_next_kind")


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
@with_data_lock
def api_migrate_units():
    from inventory_tracker import migrate_units_to_case

    inv = load_inventory()
    try:
        summary = migrate_units_to_case(inv)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc)}), 200
    save_inventory(inv)
    return jsonify({"ok": True, **summary})
