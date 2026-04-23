#!/usr/bin/env python3
"""Inventory Tracker - Flask Web GUI"""

import io
import os
from flask import Flask, jsonify, request, render_template, send_file, make_response
from inventory_tracker import (
    load_inventory, save_inventory, load_usage, save_usage,
    add_item, update_item, record_usage, restock, remove_item,
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
    return render_template("index.html")


# ---------------------------------------------------------------------------
# API – Inventory
# ---------------------------------------------------------------------------

@app.route("/api/inventory")
def api_inventory():
    inv = load_inventory()
    return jsonify(list(inv.values()))


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
    groups: dict[str, list] = {}
    for item in inv.values():
        dist = item.get("distributor") or "Unassigned"
        groups.setdefault(dist, []).append(item)

    summary = []
    for dist, items in sorted(groups.items()):
        total_qty = sum(i["quantity"] for i in items)
        total_value = sum(i["quantity"] * i["price"] for i in items)
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
                "low_stock_count": sum(1 for x in wh_items if x["quantity"] <= x["low_stock_threshold"]),
                "items": sorted(wh_items, key=lambda x: x["name"]),
            })

        summary.append({
            "distributor": dist,
            "item_count": len(items),
            "total_quantity": round(total_qty, 2),
            "total_value": round(total_value, 2),
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


@app.route("/api/email/scan", methods=["POST"])
def api_email_scan():
    from sync_inventory import scan_email

    dry_run = bool((request.json or {}).get("dry_run", False))
    try:
        report = scan_email(dry_run=dry_run)
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
            "error": str(exc),
            "messages_seen": 0,
            "messages_parsed": 0,
        }
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
