"""Inventory blueprint — SKU CRUD (list / add / update / remove), use &
restock, the usage log + reversal, and the warehouses / distributors
reference views. Extracted from app.py (refactor — see REFACTOR_PLAN.md)."""
from flask import Blueprint, jsonify, request

from inventory_tracker import (
    add_item, load_inventory, load_inventory_audit, load_usage, record_usage,
    remove_item, restock, reverse_usage, update_item,
)

inventory_bp = Blueprint("inventory", __name__)


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
    # Chefs Warehouse DCs receive their own POs but DO NOT appear in
    # the Inventory tab -- they're surfaced only on the Pending POs
    # tab via /api/chefs-warehouse/pos. CW operates four FULLY SEPARATE
    # receiving DCs (no consolidation between them; each issues its own
    # PO#s):
    #   - Bronx, NY      a.k.a. "CW NY" (Dairyland)
    #   - Chicago, IL    a.k.a. "CW Midwest"
    #   - Hanover, MD    a.k.a. "CW Mid-Atlantic"
    #   - Opa Locka, FL  a.k.a. "CW Florida"
    "Chefs Warehouse": [
        "Bronx, NY",
        "Chicago, IL",
        "Hanover, MD",
        "Opa Locka, FL",
    ],
}


def _enrich_on_order(item: dict) -> dict:
    """Add convenience fields summarising pending on_order entries.

    The inventory On Order column surfaces the *next* arrival date. For each
    pending entry we prefer the operator-confirmed ``arrival_date`` (set via
    the Pending POs ship-date flow) and fall back to the placeholder 30-day
    ``eta`` when no confirmed arrival exists yet. ``on_order_next_is_actual``
    lets the frontend distinguish confirmed arrivals from ETA estimates.
    """
    pending = item.get("on_order") or []
    total = round(sum(float(p.get("qty") or 0) for p in pending), 2)
    # Placeholder-only ETA minimum, kept for backward compatibility.
    etas = [p.get("eta", "") for p in pending if p.get("eta")]
    next_eta = min(etas) if etas else ""
    # Effective per-entry date: a confirmed arrival_date wins over the eta
    # estimate. Track whether the soonest effective date is a real arrival.
    effective = []
    for p in pending:
        arr = (p.get("arrival_date") or "").strip()
        eta = (p.get("eta") or "").strip()
        if arr:
            effective.append((arr, True))
        elif eta:
            effective.append((eta, False))
    next_arrival, next_is_actual = ("", False)
    if effective:
        next_arrival, next_is_actual = min(effective, key=lambda d: d[0])
    item["on_order_qty"] = total
    item["on_order_next_eta"] = next_eta
    item["on_order_next_arrival"] = next_arrival
    item["on_order_next_is_actual"] = bool(next_is_actual)
    return item


def _warehouse_last_count(wh_items):
    """Most recent 'count received' timestamp for a warehouse.

    A count is a rep inventory worksheet (stamps item['last_count_at']) or a
    vendor-truth on-hand true-up. Falls back to a vendor-truth last_synced so
    vendor-fed warehouses still show a date before their next worksheet lands.
    Returns an ISO string or None. ISO strings sort chronologically.
    """
    best = None
    for x in wh_items:
        ts = x.get("last_count_at")
        if not ts and x.get("last_synced_from") == "vendor-truth":
            ts = x.get("last_synced")
        if ts and (best is None or ts > best):
            best = ts
    return best


def _last_count_by_warehouse(inv):
    """Latest 'inventory received' timestamp per warehouse, derived from the
    usage history. A receipt is a rep/vendor on-hand report (usage note starts
    with 'Email on-hand sync') or a vendor-truth true-up. Deriving from history
    catches reports ingested before last_count_at stamping existed, so a
    warehouse that sent its weekly count shows green with the received date.
    Returns {warehouse: iso_timestamp}.
    """
    key_to_wh = {k: (v.get("warehouse") or "") for k, v in inv.items()}
    out: dict[str, str] = {}
    try:
        usage = load_usage()
    except Exception:  # noqa: BLE001
        return out
    for e in usage:
        note = str(e.get("note") or "")
        if e.get("source") != "vendor-truth" and not note.startswith("Email on-hand sync"):
            continue
        wh = e.get("warehouse") or key_to_wh.get(e.get("item_key") or "", "")
        ts = e.get("timestamp") or e.get("reported_at") or ""
        if wh and ts and out.get(wh, "") < ts:
            out[wh] = ts
    return out


@inventory_bp.route("/api/inventory")
def api_inventory():
    inv = load_inventory()
    return jsonify([_enrich_on_order(dict(v)) for v in inv.values()])


@inventory_bp.route("/api/inventory", methods=["POST"])
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


@inventory_bp.route("/api/inventory/<path:name>", methods=["PUT"])
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


@inventory_bp.route("/api/inventory/<path:name>", methods=["DELETE"])
def api_remove(name):
    remove_item(name)
    return jsonify({"ok": True})


@inventory_bp.route("/api/use", methods=["POST"])
def api_use():
    d = request.json
    record_usage(d["name"], float(d["amount"]), d.get("note", ""))
    return jsonify({"ok": True})


@inventory_bp.route("/api/restock", methods=["POST"])
def api_restock():
    d = request.json
    restock(d["name"], float(d["amount"]), d.get("note", ""))
    return jsonify({"ok": True})


@inventory_bp.route("/api/usage")
def api_usage():
    name = request.args.get("name")
    limit = int(request.args.get("limit", 50))
    usage = load_usage()
    if name:
        key = name.lower().strip()
        usage = [e for e in usage if e["item_key"] == key]
    usage = list(reversed(usage))[:limit]
    return jsonify(usage)


@inventory_bp.route("/api/usage/reverse", methods=["POST"])
def api_usage_reverse():
    """Undo a single usage/restock entry by its timestamp."""
    d = request.json or {}
    ts = (d.get("timestamp") or "").strip()
    if not ts:
        return jsonify({"ok": False, "error": "timestamp required"}), 400
    result = reverse_usage(ts)
    return jsonify(result)


@inventory_bp.route("/api/warehouses")
def api_warehouses():
    return jsonify(WAREHOUSES)


@inventory_bp.route("/api/distributors")
def api_distributors():
    inv = load_inventory()
    items_enriched = [_enrich_on_order(dict(v)) for v in inv.values()]
    derived_counts = _last_count_by_warehouse(inv)
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
            _lc = _warehouse_last_count(wh_items)
            _dc = derived_counts.get(wh_name)
            if _dc and (not _lc or _dc > _lc):
                _lc = _dc
            warehouses.append({
                "warehouse": wh_name,
                "last_count_at": _lc,
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


@inventory_bp.route("/api/inventory/audit")
def api_inventory_audit():
    """Recent inventory change audit trail (roadmap #9), newest-first.

    Query: ?limit=N (default 200, max 5000). Read-only; gated by the global
    auth hook (browser session or X-Inventory-Token).
    """
    try:
        limit = int(request.args.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 5000))
    return jsonify({"ok": True, "limit": limit, "audit": load_inventory_audit(limit)})
