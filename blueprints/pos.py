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
        # Pull the auto-added cases back out of on-hand.
        item["quantity"] = max(0.0, float(item.get("quantity") or 0) - qty)
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
        # Audit row (positive = reverses the original -qty restock).
        new_rows.append({
            "item_key":   ik,
            "item_name":  e.get("item_name") or item.get("name") or ik,
            "amount":     qty,
            "unit":       e.get("unit") or item.get("unit") or "",
            "note":       f"Reopened PO {po_number} -- un-rolled from Arrived",
            "timestamp":  now_iso,
            "source":     "reversal",
            "reverses_timestamp": e.get("timestamp") or "",
        })
        removed_cs += qty
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
                               "qty": qty, "unit": o.get("unit") or "cs"})

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
                                "arrival_date": "", "total_cs": 0.0, "lines": []})
        qty = abs(float(e.get("amount") or 0)); g["total_cs"] += qty
        g["lines"].append({"variety": _ledger_variety(m.get("name") or ""),
                           "qty": qty, "unit": e.get("unit") or "cs"})
        g["distributor"] = g["distributor"] or m.get("distributor") or ""
        g["warehouse"] = g["warehouse"] or m.get("warehouse") or ""
        ts = e.get("timestamp") or ""
        if ts > g["arrival_date"]:
            g["arrival_date"] = ts
    for po, g in arr.items():
        r = _rec(po); r["sources"].add("usage_rollover"); r["_arrived"] = True
        r["distributor"] = r["distributor"] or g["distributor"]
        r["warehouse"] = r["warehouse"] or g["warehouse"]
        r["ordered_at"] = r["ordered_at"] or g["ordered_at"]
        r["arrival_date"] = r["arrival_date"] or g["arrival_date"]
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
