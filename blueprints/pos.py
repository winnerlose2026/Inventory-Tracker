"""Purchase-order lifecycle blueprint — on-order ship-date edits, the Pending
POs status workflow (reopen / status-overrides / set-status), Chefs Warehouse
PO ingest + ship-date + cancel, and the arrived-POs view. Extracted from
app.py (refactor — see REFACTOR_PLAN.md). Shared helpers come from core/."""
from datetime import datetime

from flask import Blueprint, jsonify, request

# Cross-blueprint (acyclic): the Pending POs view surfaces freight-verified
# ship dates. freight imports only core/, so no cycle.
from blueprints.freight import _freight_ship_date_index
from core.errors import _safe_err
from core.util import _norm_po_key
from inventory_tracker import (
    load_inventory, load_usage, save_inventory, save_usage,
)

pos_bp = Blueprint("pos", __name__)


_ALLOWED_PO_STATUSES = {
    "open", "overdue", "in_transit", "in_production", "arrived", "cancelled",
}


def _cw_po_summary(record: dict) -> dict:
    """Shape a stored CW PO record into the same group dict the
    Pending POs frontend uses for on_order groups (so the merge in
    loadPendingPOs is shape-compatible)."""
    lines = record.get("lines") or []
    def _ln(L):
        return {
            "variety": L.get("variety") or "",
            "qty":     float(L.get("qty") or 0),
            "unit":    L.get("unit") or "cs",
            "name":    (L.get("description") or "").title(),
            "sliced":  bool(L.get("sliced")),
            "cw_item": L.get("cw_item") or "",
        }
    total_cs = float(record.get("total_cs")
                     or sum(float(L.get("qty") or 0) for L in lines))
    return {
        "po_number":    record.get("po_number") or "",
        "po_revision":  record.get("po_revision") or "",
        "distributor":  "Chefs Warehouse",
        "warehouse":    record.get("warehouse") or "",
        "dc_code":      record.get("dc_code") or "",
        "ordered_at":   record.get("ordered_at") or "",
        "eta":          record.get("eta") or "",
        "ship_date":    record.get("ship_date") or "",
        "arrival_date": record.get("arrival_date") or "",
        "buyer_name":   record.get("buyer_name") or "",
        "ship_to_name": record.get("ship_to_name") or "",
        "total_cs":     round(total_cs, 2),
        "total_usd":    record.get("total_usd"),
        "lines":        [_ln(L) for L in lines],
        "source":       record.get("source") or "",
        "source_subject": record.get("source_subject") or "",
    }


@pos_bp.route("/api/on-order/ship-date", methods=["POST"])
def api_on_order_ship_date():
    """Set (or clear) a ship_date on all pending on_order entries for a PO.

    Body:
      po_number   (required)
      ship_date   ISO date or empty/null to clear
      item_key    (optional) limit the update to a single SKU; default
                  is to update every SKU's on_order list that carries
                  this PO. Per-PO is the common case because a PO ships
                  as a whole — every line item arrives together.
      arrival_date (optional) the real delivery date, ISO. Overrides the
                  ship_date + 7 day guess and also sets eta, so the
                  Pending POs tab shows the date the operator was
                  actually given. Ignored when clearing ship_date.

    Behavior:
      - Stores ship_date on each matching entry.
      - Computes arrival_date = ship_date + 7 days and stores it, UNLESS
        an explicit arrival_date is supplied. The +7 is only a transit
        guess, and a wrong guess silently moves stock: on 2026-08-03 a
        7/27 ship date derived an 8/3 arrival and auto-promoted PO
        2055126H's 112 cs into Alcoa's on-hand a day before the truck
        actually landed (8/4). An operator who knows the real date has
        to be able to say so.
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
    arrival_raw = body.get("arrival_date")
    arrival_explicit = False

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
        if arrival_raw is not None and str(arrival_raw).strip():
            try:
                arrival_iso = datetime.fromisoformat(
                    str(arrival_raw).strip()).isoformat()
            except ValueError:
                return jsonify({
                    "ok": False,
                    "error": "arrival_date must be ISO 8601 (YYYY-MM-DD or full datetime)",
                }), 400
            arrival_explicit = True

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
            if arrival_explicit:
                # Surface the operator's date as the ETA too -- the ledger
                # reads eta off the on_order entry for the Pending POs tab.
                entry["eta"] = arrival_iso
                entry["arrival_source"] = "operator"
            updated += 1
            if item.get("name") not in touched_items:
                touched_items.append(item.get("name") or key)
    save_inventory(inv)
    return jsonify({
        "ok": True,
        "po_number": po_number,
        "ship_date": ship_iso,
        "arrival_date": arrival_iso,
        "arrival_source": "operator" if arrival_explicit else "ship+7",
        "entries_updated": updated,
        "items": touched_items,
    })


def _rollover_still_in_onhand(row: dict, item: dict) -> bool:
    """Is this rollover's qty still a SEPARATE addend sitting on top of on-hand?

    A reported on-hand count is absolute, not a delta. Once a count dated
    at-or-after the receipt's arrival has been synced, on-hand IS that count and
    the delivery is baked into it -- ``sync_inventory._receipts_after_count``
    only re-adds receipts that postdate the count. Subtracting the rollover in
    that state double-removes the cases and clamps the SKU toward zero.

    2026-08-03 is the case in point: Alcoa sat at 93 cs on a 17:46 count while
    PO 2055126H's 112 cs were still in transit (real arrival 8/4). An
    unconditional reopen would have driven the whole warehouse to ~0.
    """
    from sync_inventory import _rollover_arrival
    count_date = (item.get("last_count_at") or "")[:10]
    if not count_date:
        return True          # never counted -> the rollover is all on-hand has
    return _rollover_arrival(row) > count_date


@pos_bp.route("/api/pending/reopen", methods=["POST"])
def api_pending_reopen():
    """Reopen an Arrived PO back into the active pipeline.

    Body: { po_number (required), source: 'inventory'|'arrived'|'chefs_warehouse' }

    - Chefs Warehouse: clear ship_date + arrival_date so the date-driven
      status reverts to pending.
    - Inventory (reconstructed from a rollover): reverse the
      on_order_rollover usage rows for the PO -- removing the cases that
      were auto-added to on-hand when it rolled over (it hasn't actually
      arrived) -- and re-create the pending on_order entries so the PO
      returns to the tab as Open, awaiting a ship date.

    Freight-verified POs are locked in the UI and never reach here.
    """
    body = request.json or {}
    po_number = (body.get("po_number") or "").strip()
    source = (body.get("source") or "inventory").strip()
    if not po_number:
        return jsonify({"ok": False, "error": "po_number required"}), 400
    now_iso = datetime.now().isoformat()

    if source == "chefs_warehouse":
        from inventory_tracker import (
            load_chefs_warehouse_pos, save_chefs_warehouse_pos,
        )
        recs = load_chefs_warehouse_pos()
        hit = False
        for r in recs:
            if (r.get("po_number") or "").strip() == po_number:
                r["ship_date"] = ""
                r["arrival_date"] = ""
                hit = True
        if not hit:
            return jsonify({"ok": False, "error": "CW PO not found"}), 404
        save_chefs_warehouse_pos(recs)
        return jsonify({"ok": True, "po_number": po_number, "source": source,
                        "restored_lines": 0, "removed_cs": 0})

    # Inventory / reconstructed-arrived: un-roll the rollover.
    from inventory_tracker import (
        load_inventory, save_inventory, load_usage, save_usage,
    )
    inv = load_inventory()
    usage = load_usage()
    key_norm = _norm_po_key(po_number)
    restored = 0
    removed_cs = 0.0
    new_rows = []
    for e in usage:
        if (e.get("source") or "") != "on_order_rollover":
            continue
        if e.get("reversed"):
            continue
        if _norm_po_key(e.get("po_number") or "") != key_norm:
            continue
        ik = e.get("item_key") or ""
        item = inv.get(ik)
        qty = abs(float(e.get("amount") or 0))
        e["reversed"] = True
        e["reversed_at"] = now_iso
        if item is None or qty <= 0:
            continue
        # Pull the auto-added cases back out of on-hand -- but only while they
        # are still a separate addend there. If a count has since superseded the
        # rollover, on-hand already excludes these cases and subtracting would
        # remove them twice. See _rollover_still_in_onhand.
        still_in_onhand = _rollover_still_in_onhand(e, item)
        if still_in_onhand:
            item["quantity"] = max(0.0, float(item.get("quantity") or 0) - qty)
            removed_cs += qty
        item["updated"] = now_iso
        # Restore the pending on_order entry (no ship date yet -> Open).
        item.setdefault("on_order", []).append({
            "qty":          qty,
            "po_number":    e.get("po_number") or po_number,
            "po_revision":  e.get("po_revision") or "",
            "unit":         e.get("unit") or item.get("unit") or "cs",
            "ordered_at":   "",
            "eta":          "",
            "ship_date":    "",
            "arrival_date": "",
        })
        # Audit row (positive = reverses the original -qty restock). When a
        # count already superseded the rollover nothing moves, so the row
        # records 0 and says why -- the reversal is metadata-only.
        new_rows.append({
            "item_key":   ik,
            "item_name":  e.get("item_name") or item.get("name") or ik,
            "amount":     qty if still_in_onhand else 0.0,
            "unit":       e.get("unit") or item.get("unit") or "",
            "note":       (f"Reopened PO {po_number} -- un-rolled from Arrived"
                           if still_in_onhand else
                           f"Reopened PO {po_number} -- on-hand left on the "
                           f"{(item.get('last_count_at') or '')[:10]} count, "
                           f"which already excludes these {qty:g} cs"),
            "timestamp":  now_iso,
            "source":     "reversal",
            "reverses_timestamp": e.get("timestamp") or "",
        })
        restored += 1
    if restored:
        usage.extend(new_rows)
        save_inventory(inv)
        save_usage(usage)
    return jsonify({
        "ok": restored > 0,
        "po_number": po_number,
        "source": source,
        "restored_lines": restored,
        "removed_cs": round(removed_cs, 2),
        "error": None if restored else "No rolled-over lines found for this PO",
    })


def _parse_iso_date(raw, field):
    """Parse an ISO date/datetime, returning (iso_string, error_response)."""
    try:
        return datetime.fromisoformat(str(raw).strip()).isoformat(), None
    except ValueError:
        return None, (jsonify({
            "ok": False,
            "error": f"{field} must be ISO 8601 (YYYY-MM-DD or full datetime)",
        }), 400)


@pos_bp.route("/api/pos/arrived/adjust", methods=["POST"])
def api_arrived_po_adjust():
    """Adjust an ARRIVED inventory PO in place, without reopening it.

    Every other PO write path edits ``item["on_order"]``. Once a PO rolls over
    those rows are consumed and the PO survives only as on_order_rollover usage
    rows, so ship-date / po-order-date / remove-po all silently no-op on it and
    the only recourse was Reopen -- which un-rolls every line and blanks the
    dates, forcing a full re-key to fix one field.

    Body:
      po_number        required.
      arrival_date     ISO -- the date the truck ACTUALLY landed.
      ship_date        ISO -- record only, never moves on-hand.
      lines            [{item_key, qty}] -- quantities actually RECEIVED.
      reason           free text, recorded on every row touched.
      override_freight bool -- required to change a freight-verified ship date.

    On-hand movement follows the count-absorption rule (the same one
    _receipts_after_count and _rollover_still_in_onhand use), because a
    receipt's cases are only a separate addend on on-hand while no count has
    superseded them:

      * Moving an arrival from before a count to AFTER it means the count never
        saw those cases -> add them. This is the 2026-08-28 Chicago case: PO
        2753463T carried ETA 08-23 but landed after the 08-24 08:53 count, and
        the counted figures (3/6/41 cs) plainly excluded its 56 cs/SKU.
      * Moving an arrival from after a count to BEFORE it means the count
        already contains them -> subtract, or they are counted twice.
      * Correcting a received quantity moves on-hand only while the receipt is
        still live. Once a count has absorbed it the count IS the truth, so the
        edit is recorded against the PO but on-hand is left alone.

    Returns a per-line breakdown naming which changes moved on-hand and which
    were record-only, so the operator is never guessing.
    """
    body = request.json or {}
    po_number = (body.get("po_number") or "").strip()
    if not po_number:
        return jsonify({"ok": False, "error": "po_number required"}), 400

    reason = (body.get("reason") or "").strip()
    override_freight = bool(body.get("override_freight"))
    now_iso = datetime.now().isoformat()

    arrival_iso = ship_iso = None
    if body.get("arrival_date"):
        arrival_iso, err = _parse_iso_date(body["arrival_date"], "arrival_date")
        if err:
            return err
    if body.get("ship_date"):
        ship_iso, err = _parse_iso_date(body["ship_date"], "ship_date")
        if err:
            return err

    qty_by_key = {}
    for L in (body.get("lines") or []):
        k = (L.get("item_key") or "").strip().lower()
        if not k:
            return jsonify({"ok": False,
                            "error": "every line needs an item_key"}), 400
        try:
            q = float(L.get("qty"))
        except (TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": f"qty for {k} must be a number"}), 400
        if q < 0:
            return jsonify({"ok": False,
                            "error": f"qty for {k} cannot be negative"}), 400
        qty_by_key[k] = q

    if arrival_iso is None and ship_iso is None and not qty_by_key:
        return jsonify({"ok": False,
                        "error": "nothing to change - supply arrival_date, "
                                 "ship_date or lines"}), 400

    # The freight invoice is the trusted source for a ship date, so changing a
    # verified one is deliberate and has to say why.
    if ship_iso is not None:
        key_norm = _norm_po_key(po_number)
        if key_norm in (_freight_ship_date_index() or {}):
            if not override_freight:
                return jsonify({
                    "ok": False, "freight_verified": True,
                    "error": "This ship date is verified by a Lineage freight "
                             "invoice. Re-send with override_freight and a "
                             "reason to change it anyway.",
                }), 409
            if not reason:
                return jsonify({
                    "ok": False,
                    "error": "override_freight requires a reason",
                }), 400

    inv = load_inventory()
    usage = load_usage()
    key_norm = _norm_po_key(po_number)
    rows = [e for e in usage
            if (e.get("source") or "") == "on_order_rollover"
            and not e.get("reversed")
            and _norm_po_key(e.get("po_number") or "") == key_norm]
    if not rows:
        return jsonify({"ok": False, "error": "No arrived lines found for this "
                                              "PO - it may still be pending, "
                                              "or already reopened"}), 404

    unknown = sorted(qty_by_key) and [
        k for k in qty_by_key
        if not any((e.get("item_key") or "") == k for e in rows)
    ]
    if unknown:
        return jsonify({"ok": False,
                        "error": f"not lines on this PO: {', '.join(unknown)}"
                                 " - adding new lines is a PO revision, not "
                                 "an adjustment"}), 400

    changes = []
    audit_rows = []
    onhand_delta_total = 0.0

    for e in rows:
        ik = e.get("item_key") or ""
        item = inv.get(ik)
        old_qty = abs(float(e.get("amount") or 0))
        name = e.get("item_name") or (item.get("name") if item else ik) or ik
        entry = {"item_key": ik, "name": name, "old_qty": old_qty,
                 "new_qty": old_qty, "onhand_delta": 0.0, "record_only": False,
                 "note": ""}
        delta = 0.0

        # 1) Arrival date. Evaluate absorption BEFORE and AFTER the move; only
        #    a change in absorption state moves stock.
        if arrival_iso is not None and item is not None:
            was_live = _rollover_still_in_onhand(e, item)
            e["arrival_date"] = arrival_iso
            now_live = _rollover_still_in_onhand(e, item)
            if now_live and not was_live:
                delta += old_qty
                entry["note"] = ("arrival moved after the "
                                 f"{(item.get('last_count_at') or '')[:10]} "
                                 "count, which never saw these cases")
            elif was_live and not now_live:
                delta -= old_qty
                entry["note"] = ("arrival moved on/before the "
                                 f"{(item.get('last_count_at') or '')[:10]} "
                                 "count, which already contains these cases")
        elif arrival_iso is not None:
            e["arrival_date"] = arrival_iso

        if ship_iso is not None:
            e["ship_date"] = ship_iso
            e["ship_date_source"] = "operator-override" if override_freight \
                else "operator"

        # 2) Received quantity, judged against the FINAL arrival date.
        if ik in qty_by_key:
            new_qty = qty_by_key[ik]
            entry["new_qty"] = new_qty
            e["amount"] = -new_qty
            e["qty_adjusted_from"] = old_qty
            if item is not None and new_qty != old_qty:
                if _rollover_still_in_onhand(e, item):
                    delta += (new_qty - old_qty)
                else:
                    entry["record_only"] = True
                    entry["note"] = (entry["note"] + "; " if entry["note"] else "") + (
                        "PO record corrected; on-hand left on the "
                        f"{(item.get('last_count_at') or '')[:10]} count, which "
                        "already measured what is physically there")

        if reason:
            e["adjust_reason"] = reason
        e["adjusted_at"] = now_iso

        if item is not None and delta:
            item["quantity"] = round(float(item.get("quantity") or 0) + delta, 4)
            item["updated"] = now_iso
            onhand_delta_total += delta
            audit_rows.append({
                "item_key": ik,
                "item_name": name,
                # usage-log convention: negative amount == stock added
                "amount": -delta,
                "unit": e.get("unit") or (item.get("unit") if item else "cs"),
                "note": (f"Adjusted arrived PO {po_number}: "
                         f"{entry['note'] or 'received quantity corrected'}"
                         + (f" ({reason})" if reason else "")),
                "timestamp": now_iso,
                "po_number": po_number,
                "po_revision": e.get("po_revision") or "",
                "source": "arrived-po-adjust",
            })
        entry["onhand_delta"] = round(delta, 4)
        changes.append(entry)

    if audit_rows:
        usage.extend(audit_rows)
    save_inventory(inv)
    save_usage(usage)

    return jsonify({
        "ok": True,
        "po_number": po_number,
        "arrival_date": arrival_iso,
        "ship_date": ship_iso,
        "lines_touched": len(rows),
        "onhand_delta_cs": round(onhand_delta_total, 4),
        "freight_overridden": bool(ship_iso is not None and override_freight),
        "changes": changes,
    })


@pos_bp.route("/api/pending/status-overrides")
def api_pending_status_overrides():
    """Return the manual Pending-PO status overrides {normPOkey: status}."""
    from inventory_tracker import load_status_overrides
    return jsonify({"ok": True, "overrides": load_status_overrides()})


@pos_bp.route("/api/pending/set-status", methods=["POST"])
def api_pending_set_status():
    """Set or clear a manual status override for a PO.

    Body: { po_number (required), status }
      status in {open, overdue, in_transit, in_production, arrived,
      cancelled}; empty / "auto" clears the override (back to computed).

    Display-only: the override forces the tag shown on the Pending POs tab
    (winning over the 30-day ETA / freight rules). It does NOT move
    inventory on-hand.
    """
    from inventory_tracker import load_status_overrides, save_status_overrides
    body = request.json or {}
    po_number = (body.get("po_number") or "").strip()
    status = (body.get("status") or "").strip().lower()
    if not po_number:
        return jsonify({"ok": False, "error": "po_number required"}), 400
    if status in ("", "auto"):
        status = ""
    elif status not in _ALLOWED_PO_STATUSES:
        return jsonify({"ok": False,
                        "error": f"status must be one of {sorted(_ALLOWED_PO_STATUSES)} or empty"}), 400
    key = _norm_po_key(po_number)
    overrides = load_status_overrides()
    if status:
        overrides[key] = status
    else:
        overrides.pop(key, None)
    save_status_overrides(overrides)
    return jsonify({"ok": True, "po_number": po_number, "key": key,
                    "status": status or None})


@pos_bp.route("/api/chefs-warehouse/pos")
def api_chefs_warehouse_pos():
    """List all Chefs Warehouse POs (active + arrived).

    Query params:
      ``status``  filter to "pending" (default), "arrived", "canceled",
                  or "all".

    A CW PO is "pending" until an operator sets a ship_date or
    arrival_date that's in the past, or marks it canceled. The Pending
    POs tab fetches the default (pending only).
    """
    from inventory_tracker import load_chefs_warehouse_pos, load_canceled_pos
    records = load_chefs_warehouse_pos()
    canceled = load_canceled_pos()

    status_filter = (request.args.get("status") or "pending").lower()
    freight_idx = _freight_ship_date_index()

    out = []
    now = datetime.now()
    for r in records:
        po_num = (r.get("po_number") or "").strip()
        if r.get("canceled") or po_num in canceled:
            status = "canceled"
        else:
            # Auto-ETA rule (2026-05-27): CW POs only auto-flip to
            # "arrived" off an OPERATOR-set arrival_date. The parser-set
            # eta (from the PDF's printed delivery date or a 30-day
            # fallback) is ignored for status — the vendor's promise of
            # when they'll deliver is not the same as confirmation that
            # the truck showed up. Operator types ship_date -> the
            # ship-date endpoint stores arrival_date = ship_date + 7d,
            # and that's what flips us to "arrived" past its date.
            arrival_str = (r.get("arrival_date") or "").strip()
            arrival_dt = None
            if arrival_str:
                try:
                    arrival_dt = datetime.fromisoformat(arrival_str)
                except ValueError:
                    arrival_dt = None
            if arrival_dt and arrival_dt <= now:
                status = "arrived"
            else:
                status = "pending"

        if status_filter != "all" and status != status_filter:
            continue

        item = _cw_po_summary(r)
        item["status"] = status
        # Backfill a missing ship date on ARRIVED CW POs from the freight
        # invoice index (links by PO #). Display-only; never persisted and
        # never recomputes arrival, so the computed status is untouched.
        if status == "arrived" and not (item.get("ship_date") or "").strip():
            sd = freight_idx.get(_norm_po_key(po_num))
            if sd:
                item["ship_date"] = sd
                item["ship_date_source"] = "freight"
        out.append(item)

    out.sort(key=lambda x: (x.get("ordered_at") or "", x.get("po_number") or ""))
    return jsonify({"ok": True, "count": len(out), "pos": out})


@pos_bp.route("/api/chefs-warehouse/ingest-pos", methods=["POST"])
def api_chefs_warehouse_ingest_pos():
    """Accept externally-parsed CW PO records and apply them.

    Used by the Cowork scheduled routine that fetches Graph mail
    directly: it pulls CW PDFs, parses them with the same parser, and
    POSTs the dict-form records here. Mirrors /api/email/ingest-events
    but for the CW-only channel.

    Request body:
        {
          "dry_run": false,
          "source":  "cowork-routine",
          "cw_pos":  [ <ChefsWarehousePO as dict>, ... ]
        }
    """
    import traceback as _tb
    try:
        from sync_inventory import _apply_cw_pos
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False,
                        "error": _safe_err(exc, "import")}), 500
    body = request.json or {}
    dry_run = bool(body.get("dry_run", False))
    source = str(body.get("source") or "external").strip() or "external"
    cw_pos_raw = body.get("cw_pos") or []
    if not isinstance(cw_pos_raw, list):
        return jsonify({"ok": False, "error": "cw_pos must be a list"}), 400
    try:
        report = _apply_cw_pos(cw_pos_raw, dry_run=dry_run, source=source)
    except Exception as exc:  # noqa: BLE001
        return jsonify({
            "ok": False,
            "error": _safe_err(exc),
            "traceback": "",
        }), 500
    return jsonify({"ok": True, "dry_run": dry_run, "report": report})


@pos_bp.route("/api/chefs-warehouse/ship-date", methods=["POST"])
def api_chefs_warehouse_ship_date():
    """Set / clear ship_date (and the derived arrival_date) on a CW PO.

    Body: ``{"po_number": "...", "ship_date": "YYYY-MM-DD" | ""}``.
    arrival_date is set to ship_date + 7 days (CW transit lead) to
    match the on_order convention; clearing ship_date also clears it.
    """
    from datetime import timedelta
    from inventory_tracker import (
        load_chefs_warehouse_pos, save_chefs_warehouse_pos,
    )
    body = request.json or {}
    po_number = (body.get("po_number") or "").strip()
    ship_iso = (body.get("ship_date") or "").strip()
    if not po_number:
        return jsonify({"ok": False, "error": "po_number required"}), 400

    if ship_iso:
        try:
            ship_dt = datetime.fromisoformat(ship_iso)
        except ValueError:
            return jsonify({"ok": False,
                            "error": "ship_date must be YYYY-MM-DD"}), 400
        arrival_iso = (ship_dt + timedelta(days=7)).isoformat()
    else:
        ship_iso = ""
        arrival_iso = ""

    records = load_chefs_warehouse_pos()
    found = False
    for r in records:
        if (r.get("po_number") or "").strip() != po_number:
            continue
        r["ship_date"]    = ship_iso
        r["arrival_date"] = arrival_iso
        r["updated_at"]   = datetime.now().isoformat()
        found = True
    if not found:
        return jsonify({"ok": False,
                        "error": f"PO {po_number} not found in CW POs"}), 404
    save_chefs_warehouse_pos(records)
    return jsonify({
        "ok": True,
        "po_number": po_number,
        "ship_date": ship_iso,
        "arrival_date": arrival_iso,
    })


@pos_bp.route("/api/chefs-warehouse/cancel", methods=["POST"])
def api_chefs_warehouse_cancel():
    """Mark a CW PO canceled. Removes it from the default Pending POs
    list and adds the PO# to the shared canceled-POs ignore list so a
    re-scan of the same source email doesn't re-add it.
    """
    from inventory_tracker import (
        load_chefs_warehouse_pos, save_chefs_warehouse_pos,
        load_canceled_pos, save_canceled_pos,
    )
    body = request.json or {}
    po_number = (body.get("po_number") or "").strip()
    reason = (body.get("reason") or "canceled by distributor").strip()
    if not po_number:
        return jsonify({"ok": False, "error": "po_number required"}), 400

    records = load_chefs_warehouse_pos()
    found = False
    for r in records:
        if (r.get("po_number") or "").strip() != po_number:
            continue
        r["canceled"]        = True
        r["canceled_at"]     = datetime.now().isoformat(timespec="seconds")
        r["canceled_reason"] = reason
        found = True
    save_chefs_warehouse_pos(records)

    canceled = load_canceled_pos()
    canceled[po_number] = {
        "canceled_at": datetime.now().isoformat(timespec="seconds"),
        "reason":      reason,
        "distributor": "Chefs Warehouse",
    }
    save_canceled_pos(canceled)

    return jsonify({
        "ok": True,
        "po_number": po_number,
        "found_in_cw_file": found,
        "added_to_ignore_list": True,
    })


@pos_bp.route("/api/arrived-pos")
def api_arrived_pos():
    """List arrived inventory-side POs reconstructed from the usage log.

    Groups usage rows with source=="on_order_rollover" by po_number and
    enriches each group with the distributor / warehouse pulled from the
    SKU's current inventory record. Chefs Warehouse POs are intentionally
    excluded here — they're served (arrived + active) by
    /api/chefs-warehouse/pos.

    Each group matches the shape the Pending POs frontend expects:
      po_number, po_revision, distributor, warehouse, ordered_at (best
      effort), eta, ship_date, arrival_date, total_cs, lines[], status.
    """
    import re as _re
    from inventory_tracker import load_inventory, load_usage

    inv = load_inventory()
    meta = {}
    for key, item in inv.items():
        meta[key] = {
            "distributor": item.get("distributor") or "",
            "warehouse":   item.get("warehouse") or "",
            "name":        item.get("name") or key,
        }

    groups: dict = {}
    for e in (load_usage() or []):
        if (e.get("source") or "") != "on_order_rollover":
            continue
        if e.get("reversed"):
            continue
        po = (e.get("po_number") or "").strip()
        if not po:
            continue
        m = meta.get(e.get("item_key") or "", {})
        g = groups.get(po)
        if g is None:
            g = groups[po] = {
                "po_number":    po,
                "po_revision":  e.get("po_revision") or "",
                "distributor":  m.get("distributor") or "",
                "warehouse":    m.get("warehouse") or "",
                "ordered_at":   e.get("ordered_at") or "",
                "eta":          "",
                "ship_date":    e.get("ship_date") or "",
                "arrival_date": e.get("timestamp") or "",
                "total_cs":     0.0,
                "lines":        [],
                "status":       "arrived",
            }
        qty = abs(float(e.get("amount") or 0))
        g["total_cs"] += qty
        name = m.get("name") or e.get("item_name") or ""
        variety = name.split(" Bagel")[0] if " Bagel" in name else name
        g["lines"].append({
            "variety": variety,
            "name":    name,
            "qty":     qty,
            "unit":    e.get("unit") or "cs",
        })
        # arrival_date = the latest rollover timestamp across the PO's lines.
        ts = e.get("timestamp") or ""
        if ts > (g["arrival_date"] or ""):
            g["arrival_date"] = ts
        # First non-empty distributor / warehouse wins (lines may map to
        # SKUs that lost their metadata; keep the first useful one).
        if not g["distributor"] and m.get("distributor"):
            g["distributor"] = m["distributor"]
        if not g["warehouse"] and m.get("warehouse"):
            g["warehouse"] = m["warehouse"]
        # The rollover note carries "(ETA YYYY-MM-DD)" — surface it.
        if not g["eta"]:
            mm = _re.search(r"ETA (\d{4}-\d{2}-\d{2})", e.get("note") or "")
            if mm:
                g["eta"] = mm.group(1)

    out = list(groups.values())
    for g in out:
        g["total_cs"] = round(g["total_cs"], 2)

    # Backfill missing ship dates from the Lineage freight invoices,
    # linked by PO number. Display-only enrichment — nothing is persisted.
    freight_idx = _freight_ship_date_index()
    backfilled = 0
    for g in out:
        if (g.get("ship_date") or "").strip():
            continue
        sd = freight_idx.get(_norm_po_key(g.get("po_number") or ""))
        if sd:
            g["ship_date"] = sd
            g["ship_date_source"] = "freight"
            backfilled += 1

    # Newest arrivals first.
    out.sort(key=lambda x: (x.get("arrival_date") or "", x.get("po_number") or ""),
             reverse=True)
    return jsonify({"ok": True, "count": len(out),
                    "ship_date_backfilled": backfilled, "pos": out})


def _present_po_numbers() -> set:
    """Every PO number visible on the Pending POs dashboard: on_order (pending
    USF/Cheney) + on_order_rollover usage rows (arrived USF/Cheney) + the Chefs
    Warehouse store (all statuses). Read-only."""
    from inventory_tracker import (
        load_inventory, load_usage, load_chefs_warehouse_pos,
    )
    present = set()
    for item in load_inventory().values():
        for o in (item.get("on_order") or []):
            po = str(o.get("po_number") or "").strip()
            if po:
                present.add(po)
    for e in (load_usage() or []):
        if (e.get("source") or "") == "on_order_rollover" and not e.get("reversed"):
            po = str(e.get("po_number") or "").strip()
            if po:
                present.add(po)
    for r in load_chefs_warehouse_pos():
        po = str(r.get("po_number") or "").strip()
        if po:
            present.add(po)
    return present


@pos_bp.route("/api/pos/reconcile", methods=["POST"])
def api_pos_reconcile():
    """Read-only PO gap check / reconciliation (missing-PO alert).

    POST {"pos": [{"po_number", "distributor", "warehouse", "date"}, ...]} -- the
    EXPECTED set of POs (e.g. parsed from H&H's net-chef invoice PDFs). Returns
    which expected POs are already on the Pending POs dashboard (pending,
    arrived, or in the Chefs Warehouse store) and which are MISSING (slipped
    through). Ingests nothing and never modifies data -- it only flags gaps so
    they can be reconciled.
    """
    from inventory_tracker import reconcile_po_list
    body = request.json or {}
    expected = body.get("pos") or []
    if not isinstance(expected, list):
        return jsonify({"ok": False, "error": "pos must be a list"}), 400
    present_set = _present_po_numbers()
    present, missing = reconcile_po_list(expected, present_set)
    return jsonify({
        "ok": True,
        "expected_count": len(expected),
        "present_count": len(present),
        "missing_count": len(missing),
        "missing": missing,
        "dashboard_po_count": len(present_set),
    })


# ---------------------------------------------------------------------------
# Canonical PO ledger (Phase 2 of the data consolidation / production planner).
# ONE record per PO assembled from every source the dashboard unions today:
# pending USF/Cheney (inventory on_order), arrived USF/Cheney (usage rollover),
# and the Chefs Warehouse store -- plus canceled/override status, freight
# actual ship dates, provenance, and the warehouse transfer group. Read-only;
# the single source-of-truth VIEW the planner consumes. Write paths are
# unchanged (Phase 2b will migrate them onto this ledger and retire the
# fragments).
# ---------------------------------------------------------------------------

def _ledger_variety(name: str) -> str:
    name = name or ""
    return name.split(" Bagel")[0].strip() if " Bagel" in name else name


def _date_le(iso_s: str, now) -> bool:
    iso_s = (iso_s or "").strip()
    if not iso_s:
        return False
    try:
        return datetime.fromisoformat(iso_s) <= now
    except ValueError:
        return False


def build_po_ledger() -> list:
    """Assemble one canonical record per PO across all sources. Pure read."""
    from inventory_tracker import (
        load_inventory, load_usage, load_chefs_warehouse_pos,
        load_canceled_pos, load_status_overrides,
    )
    from integrations.planning_config import transfer_group_for

    now = datetime.now()
    canceled = load_canceled_pos() or {}
    overrides = load_status_overrides() or {}
    freight_idx = _freight_ship_date_index()
    recs: dict = {}

    def _rec(po):
        return recs.setdefault(po, {
            "po_number": po, "po_revision": "", "distributor": "", "warehouse": "",
            "ordered_at": "", "eta": "", "ship_date": "", "ship_date_source": "",
            "arrival_date": "", "total_cs": 0.0, "lines": [], "sources": set(),
            "dc_code": "",
            "_pending": False, "_arrived": False, "_canceled": False,
        })

    # 1) pending USF/Cheney -- inventory on_order
    inv = load_inventory()
    for key, item in inv.items():
        for o in (item.get("on_order") or []):
            po = (o.get("po_number") or "").strip()
            if not po:
                continue
            r = _rec(po); r["sources"].add("on_order"); r["_pending"] = True
            r["distributor"] = r["distributor"] or (item.get("distributor") or "")
            r["warehouse"] = r["warehouse"] or (item.get("warehouse") or "")
            r["po_revision"] = r["po_revision"] or (o.get("po_revision") or "")
            r["ordered_at"] = r["ordered_at"] or (o.get("ordered_at") or "")
            r["eta"] = r["eta"] or (o.get("eta") or "")
            if o.get("ship_date") and not r["ship_date"]:
                r["ship_date"] = o["ship_date"]; r["ship_date_source"] = "operator"
            if o.get("arrival_date") and not r["arrival_date"]:
                r["arrival_date"] = o["arrival_date"]
            qty = float(o.get("qty") or 0); r["total_cs"] += qty
            r["lines"].append({"variety": _ledger_variety(item.get("name") or key),
                               "qty": qty, "unit": o.get("unit") or "cs",
                               "item_key": key})

    # 2) arrived USF/Cheney -- usage rollover rows grouped by PO
    meta = {k: {"distributor": it.get("distributor") or "",
                "warehouse": it.get("warehouse") or "",
                "name": it.get("name") or k} for k, it in inv.items()}
    arr: dict = {}
    for e in (load_usage() or []):
        if (e.get("source") or "") != "on_order_rollover" or e.get("reversed"):
            continue
        po = (e.get("po_number") or "").strip()
        if not po:
            continue
        m = meta.get(e.get("item_key") or "", {})
        g = arr.setdefault(po, {"distributor": "", "warehouse": "",
                                "ordered_at": e.get("ordered_at") or "",
                                "arrival_date": "", "ship_date": "", "eta": "",
                                "total_cs": 0.0, "lines": []})
        qty = abs(float(e.get("amount") or 0)); g["total_cs"] += qty
        g["lines"].append({"variety": _ledger_variety(m.get("name") or ""),
                           "qty": qty, "unit": e.get("unit") or "cs",
                           "item_key": e.get("item_key") or ""})
        g["distributor"] = g["distributor"] or m.get("distributor") or ""
        g["warehouse"] = g["warehouse"] or m.get("warehouse") or ""
        ts = e.get("timestamp") or ""
        if ts > g["arrival_date"]:
            g["arrival_date"] = ts
        # The rollover row carries the ship date the PO was booked with, and the
        # trigger date it arrived on. Without these the Pending POs tab renders
        # an arrived PO with an arrival date but a blank Ship column, even though
        # /api/arrived-pos has always surfaced both from the same rows.
        if not g["ship_date"] and e.get("ship_date"):
            g["ship_date"] = e["ship_date"]
        if not g["eta"]:
            g["eta"] = (e.get("arrival_date") or "").strip()
    for po, g in arr.items():
        r = _rec(po); r["sources"].add("usage_rollover"); r["_arrived"] = True
        r["distributor"] = r["distributor"] or g["distributor"]
        r["warehouse"] = r["warehouse"] or g["warehouse"]
        r["ordered_at"] = r["ordered_at"] or g["ordered_at"]
        r["arrival_date"] = r["arrival_date"] or g["arrival_date"]
        r["eta"] = r["eta"] or g["eta"]
        if g["ship_date"] and not r["ship_date"]:
            r["ship_date"] = g["ship_date"]; r["ship_date_source"] = "operator"
        if not r["_pending"]:   # use the arrived snapshot only if not still pending
            r["total_cs"] = g["total_cs"]; r["lines"] = g["lines"]

    # 3) Chefs Warehouse store
    for cw in load_chefs_warehouse_pos():
        sm = _cw_po_summary(cw)
        po = (sm.get("po_number") or "").strip()
        if not po:
            continue
        r = _rec(po); r["sources"].add("cw_store")
        r["distributor"] = "Chefs Warehouse"
        r["warehouse"] = r["warehouse"] or sm.get("warehouse") or ""
        r["po_revision"] = r["po_revision"] or sm.get("po_revision") or ""
        r["ordered_at"] = r["ordered_at"] or sm.get("ordered_at") or ""
        r["eta"] = r["eta"] or sm.get("eta") or ""
        if sm.get("ship_date") and not r["ship_date"]:
            r["ship_date"] = sm["ship_date"]; r["ship_date_source"] = "operator"
        if sm.get("arrival_date") and not r["arrival_date"]:
            r["arrival_date"] = sm["arrival_date"]
        r["total_cs"] = sm.get("total_cs") or r["total_cs"]
        r["lines"] = sm.get("lines") or r["lines"]
        r["dc_code"] = r["dc_code"] or (sm.get("dc_code") or "")
        if cw.get("canceled"):
            r["_canceled"] = True

    # 4) freight actual ship dates (authoritative) + status + transfer group
    out = []
    for po, r in recs.items():
        sd = freight_idx.get(_norm_po_key(po))
        if sd:
            r["ship_date"] = sd; r["ship_date_source"] = "freight"; r["sources"].add("freight")
        ov = overrides.get(po)
        if r["_canceled"] or po in canceled or ov in ("cancelled", "canceled"):
            status = "canceled"
        elif ov:
            status = ov
        elif r["_arrived"] or _date_le(r.get("arrival_date"), now):
            status = "arrived"
        elif r.get("ship_date"):
            status = "in_transit"
        else:
            status = "pending"
        r["status"] = status
        r["override"] = ov or ""
        _ss = r["sources"]
        r["source_kind"] = ("chefs_warehouse" if "cw_store" in _ss
                             else "arrived" if ("usage_rollover" in _ss and "on_order" not in _ss)
                             else "inventory")
        r["transfer_group"] = transfer_group_for(r.get("warehouse") or "")
        r["total_cs"] = round(float(r.get("total_cs") or 0), 2)
        r["sources"] = sorted(r["sources"])
        for k in ("_pending", "_arrived", "_canceled"):
            r.pop(k, None)
        out.append(r)
    out.sort(key=lambda x: (x.get("status") or "", x.get("ordered_at") or "", x.get("po_number") or ""))
    return out


@pos_bp.route("/api/pos/ledger")
def api_pos_ledger():
    """Canonical PO ledger -- one record per PO across pending (on_order),
    arrived (usage rollover), and the Chefs Warehouse store, with status,
    provenance, freight-actual ship dates, and transfer group. Optional
    ?status= and ?distributor= filters. Read-only; gated by the auth hook."""
    ledger = build_po_ledger()
    by_status = {}
    by_distributor = {}
    for r in ledger:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        d = r.get("distributor") or "?"
        by_distributor[d] = by_distributor.get(d, 0) + 1
    status = (request.args.get("status") or "").strip().lower() or None
    dist = (request.args.get("distributor") or "").strip().lower() or None
    if status:
        ledger = [r for r in ledger if (r.get("status") or "").lower() == status]
    if dist:
        ledger = [r for r in ledger if (r.get("distributor") or "").lower() == dist]
    return jsonify({"ok": True, "count": len(ledger),
                    "by_status": by_status, "by_distributor": by_distributor,
                    "pos": ledger})
