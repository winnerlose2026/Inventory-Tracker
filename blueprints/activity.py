"""Activity Log + Pending POs <-> Inventory flow check.

Two read-only views built for 2026-10-23, after La Mirada PO 7135734C showed
"In Production" on the Pending POs tab while the Inventory tab had nothing on
order and on-hand already carried its 336 cs:

``GET /api/activity/flow-check``
    For every US Foods / Cheney warehouse, do the open POs on the Pending POs
    tab (the canonical ledger) match what the Inventory tab carries on order?
    Lists every PO where they disagree, and why. Chefs Warehouse is reported
    separately: by design its POs never enter the Inventory tab.

``GET /api/activity``
    One timeline of every change flowing through, newest first, merged from
    the three places changes are recorded:
      * inventory_audit.json -- what each scan applied (counts, PO bookings,
        PO replacements, usage rates, admin corrections);
      * usage.json -- PO arrivals into on-hand, arrivals a count already
        held, un-rolls / reversals, manual moves and (optionally) the daily
        forecast burn;
      * activity_log.jsonl (core/activity.py) -- every change request: status
        and date edits, cancels, reopens, scans, ingests, follow-ups, with who
        sent it.
    Per-SKU rows of one event (a 13-SKU count, a 9-line PO arrival) are
    grouped into one line with the SKUs as detail.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from core.errors import _log_exc
from core.util import (NOT_ARRIVED_STATUSES, _norm_po_key, absorbed_row_qty,
                       rollover_row_live)

activity_bp = Blueprint("activity", __name__)

_INVENTORY_DISTRIBUTORS = ("US Foods", "Cheney Brothers")


# ---------------------------------------------------------------------------
# Flow check
# ---------------------------------------------------------------------------

def po_flow_check(ledger=None, inv=None, usage=None, overrides=None, now=None) -> dict:
    """Compare the Pending POs ledger with the Inventory tab's on-order.

    Pure when every input is passed (tests); otherwise loads them.
    """
    from inventory_tracker import (load_inventory, load_usage,
                                   load_status_overrides, _parse_sane_dt)
    if ledger is None:
        from blueprints.pos import build_po_ledger
        ledger = build_po_ledger()
    inv = load_inventory() if inv is None else inv
    usage = load_usage() if usage is None else usage
    overrides = (load_status_overrides() or {}) if overrides is None else overrides
    now = now or datetime.now()

    # Inventory side: on-order cs per (PO, warehouse), and per warehouse.
    on_order: dict = {}
    wh_meta: dict = {}
    issues, notes = [], []
    for key, item in inv.items():
        wh = item.get("warehouse") or ""
        dist = item.get("distributor") or ""
        if dist in _INVENTORY_DISTRIBUTORS and wh:
            wh_meta.setdefault(wh, dist)
        for o in (item.get("on_order") or []):
            po = (o.get("po_number") or "").strip()
            qty = float(o.get("qty") or 0)
            if not po:
                issues.append({"kind": "on_order_without_po", "po_number": "",
                               "distributor": dist, "warehouse": wh,
                               "detail": f"{item.get('name') or key}: {qty:g} cs on "
                                         "order with no PO number -- it cannot "
                                         "appear on the Pending POs tab"})
                continue
            k = (_norm_po_key(po), wh)
            on_order[k] = on_order.get(k, 0.0) + qty
            ov = overrides.get(_norm_po_key(po))
            eta = _parse_sane_dt(o.get("eta") or "")
            if (ov in NOT_ARRIVED_STATUSES and not (o.get("arrival_date") or "").strip()
                    and eta is not None and eta <= now):
                notes.append({"kind": "held_by_status", "po_number": po,
                              "distributor": dist, "warehouse": wh,
                              "detail": f"Held on order by its '{ov.replace('_', ' ')}' "
                                        f"status past the 30-day ETA "
                                        f"{eta.date().isoformat()} -- enter a ship "
                                        "date when it is known"})

    # Ledger side.
    seen = set()
    wh_rows: dict = {}
    cw: dict = {}
    for r in ledger:
        po = r.get("po_number") or ""
        wh = r.get("warehouse") or ""
        dist = r.get("distributor") or ""
        status = r.get("status") or ""
        open_ = status not in ("arrived", "canceled")
        if dist == "Chefs Warehouse":
            if open_:
                c = cw.setdefault(wh, {"warehouse": wh, "dc_code": r.get("dc_code") or "",
                                       "open_pos": 0, "open_cs": 0.0})
                c["open_pos"] += 1
                c["open_cs"] += float(r.get("total_cs") or 0)
            continue
        if dist and dist not in _INVENTORY_DISTRIBUTORS:
            continue
        k = (_norm_po_key(po), wh)
        seen.add(k)
        pend_cs = sum(float(L.get("qty") or 0) for L in (r.get("lines") or [])
                      if not L.get("arrived"))
        inv_cs = on_order.get(k, 0.0)
        w = wh_rows.setdefault(wh, {"distributor": dist, "warehouse": wh,
                                    "open_pos": 0, "pending_pos_cs": 0.0,
                                    "inventory_on_order_cs": 0.0})
        base = {"po_number": po, "distributor": dist, "warehouse": wh,
                "ledger_status": status, "ledger_cs": round(pend_cs, 2),
                "on_order_cs": round(inv_cs, 2)}
        if open_:
            w["open_pos"] += 1
            if r.get("source_kind") == "arrived":
                # Every line already rolled into on-hand, yet the tab says it
                # has not arrived (a pinned status, or a future arrival date).
                pend_cs = float(r.get("total_cs") or 0)
                base["ledger_cs"] = round(pend_cs, 2)
                issues.append(dict(base, kind="open_but_in_on_hand",
                                   detail=f"Pending POs shows it as "
                                          f"{status.replace('_', ' ')}, but its "
                                          f"{pend_cs:g} cs were already added to "
                                          "on-hand and nothing is on order. Set its "
                                          "status again (or Reopen) to move it back "
                                          "on order."))
            elif abs(pend_cs - inv_cs) > 0.01:
                issues.append(dict(base, kind="qty_mismatch",
                                   detail=f"Pending POs: {pend_cs:g} cs open; "
                                          f"Inventory on order: {inv_cs:g} cs"))
            w["pending_pos_cs"] += pend_cs
        elif status == "arrived" and inv_cs > 0.01:
            issues.append(dict(base, kind="arrived_but_on_order",
                               detail=f"Pending POs shows it Arrived, but "
                                      f"{inv_cs:g} cs are still on order in "
                                      "Inventory (status set without an arrival "
                                      "date?)"))
        w["inventory_on_order_cs"] += inv_cs

    for (pk, wh), qty in on_order.items():
        if (pk, wh) not in seen:
            issues.append({"kind": "on_order_not_on_pending_tab", "po_number": pk,
                           "distributor": wh_meta.get(wh, ""), "warehouse": wh,
                           "ledger_status": "", "ledger_cs": 0.0,
                           "on_order_cs": round(qty, 2),
                           "detail": f"{qty:g} cs on order in Inventory but the "
                                     "PO is not on the Pending POs tab"})
            w = wh_rows.setdefault(wh, {"distributor": wh_meta.get(wh, ""),
                                        "warehouse": wh, "open_pos": 0,
                                        "pending_pos_cs": 0.0,
                                        "inventory_on_order_cs": 0.0})
            w["inventory_on_order_cs"] += qty

    # Arrival rows dated in the future: rolled into on-hand "early".
    for e in usage:
        if not rollover_row_live(e):
            continue
        a = _parse_sane_dt(e.get("arrival_date") or "")
        if a is not None and a > now:
            po = e.get("po_number") or ""
            if not any(i.get("po_number") == po and i["kind"] == "arrived_future_date"
                       for i in issues):
                item = inv.get(e.get("item_key") or "") or {}
                issues.append({"kind": "arrived_future_date", "po_number": po,
                               "distributor": item.get("distributor") or "",
                               "warehouse": e.get("warehouse") or item.get("warehouse") or "",
                               "detail": f"Counted in on-hand, but its arrival date "
                                         f"{a.date().isoformat()} is still ahead"})

    bad_wh = {i.get("warehouse") for i in issues}
    warehouses = []
    for wh in sorted(set(wh_meta) | set(wh_rows)):
        w = wh_rows.get(wh) or {"distributor": wh_meta.get(wh, ""), "warehouse": wh,
                                "open_pos": 0, "pending_pos_cs": 0.0,
                                "inventory_on_order_cs": 0.0}
        w["distributor"] = w.get("distributor") or wh_meta.get(wh, "")
        w["pending_pos_cs"] = round(w["pending_pos_cs"], 2)
        w["inventory_on_order_cs"] = round(w["inventory_on_order_cs"], 2)
        w["ok"] = (wh not in bad_wh and abs(w["pending_pos_cs"]
                                            - w["inventory_on_order_cs"]) <= 0.01)
        warehouses.append(w)
    for c in cw.values():
        c["open_cs"] = round(c["open_cs"], 2)
    return {
        "ok": not issues,
        "checked_at": now.isoformat(timespec="seconds"),
        "issue_count": len(issues),
        "issues": issues,
        "notes": notes,
        "warehouses": warehouses,
        "chefs_warehouse": sorted(cw.values(), key=lambda c: c["warehouse"]),
    }


@activity_bp.route("/api/activity/flow-check")
def api_flow_check():
    try:
        return jsonify(po_flow_check())
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "po flow check")
        return jsonify({"ok": False, "error": "internal error"}), 500


# ---------------------------------------------------------------------------
# Activity timeline
# ---------------------------------------------------------------------------

# Audit event_type -> (category, label)
_AUDIT_KINDS = {
    "on_hand": ("count", "Count received"),
    "on_order": ("po", "PO booked on order"),
    "on_order_reversed": ("po", "PO booking replaced by a newer copy"),
    "po_reversal": ("po", "Arrived PO taken back out (newer copy)"),
    "usage_rate": ("usage", "Weekly usage updated"),
    "correction": ("correction", "Correction"),
    "restock": ("po", "Restock"),
    "production_repair": ("correction", "Production repair"),
}

# usage.json source -> (category, label)
_USAGE_KINDS = {
    "on_order_rollover": ("arrival", "PO arrived → added to on-hand"),
    "on_order_absorbed": ("arrival", "PO arrived — already in the count"),
    "reversal": ("po", "Moved back on order / reversed"),
    "arrived-po-adjust": ("correction", "Arrived PO quantity corrected"),
    "forecast-daily": ("burn", "Daily usage burn (forecast)"),
    "manual-use": ("correction", "Manual use"),
    "manual-restock": ("correction", "Manual restock"),
    "manual-correction": ("correction", "Manual correction"),
    "historical-backfill": ("burn", "Historical usage backfill"),
}

CATEGORIES = ("count", "po", "arrival", "action", "usage", "correction", "burn")


def _norm_ts(raw: str) -> str:
    """Stored stamps mix naive local and '...+00:00'/Z; compare as naive local."""
    from inventory_tracker import _parse_sane_dt
    dt = _parse_sane_dt(raw or "")
    return dt.isoformat(timespec="seconds") if dt else (raw or "")[:19]


def _usage_kind(e: dict):
    src = e.get("source") or ""
    if src in _USAGE_KINDS:
        return _USAGE_KINDS[src]
    if "reversal_of_revision" in e:
        return ("po", "Arrived PO taken back out (newer copy)")
    note = e.get("note") or ""
    if note.startswith("Email on-hand sync"):
        return None                          # the audit's count rows cover these
    if note.startswith("Correction"):
        return ("correction", "Correction")
    if src.startswith("admin"):
        return ("correction", src.replace("-", " ").capitalize())
    return None


def _num(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:g}" if abs(f - round(f)) > 1e-9 else f"{int(round(f))}"


_RESULT_TEXT = {
    "moved_back_cs": lambda v: f"moved back on order: {_num(v)} cs",
    "onhand_removed_cs": lambda v: f"on-hand \u2212{_num(v)} cs" if float(v or 0) else
                                   "on-hand unchanged (a count already covers it)",
    "removed_cs": lambda v: f"on-hand \u2212{_num(v)} cs",
    "onhand_delta_cs": lambda v: (f"on-hand +{_num(v)} cs" if float(v) > 0
                                  else f"on-hand \u2212{_num(abs(float(v)))} cs"),
    "restored_lines": lambda v: f"{_num(v)} line(s) back on order",
    "arrival_stamped_lines": lambda v: f"arrival stamped on {_num(v)} line(s)",
    "unrolled": lambda v: "not arrived yet \u2014 moved back on order",
    "error": lambda v: f"error: {v}",
}


def _action_bits(req: dict, res: dict) -> list:
    """Readable one-liners for an action-log entry (request, then result)."""
    bits = []
    if req.get("status") not in (None, ""):
        bits.append(f"status \u2192 {str(req['status']).replace('_', ' ')}")
    elif "status" in req:
        bits.append("status \u2192 auto")
    for k, lab in (("ship_date", "ship"), ("arrival_date", "arrival")):
        if req.get(k):
            bits.append(f"{lab} {str(req[k])[:10]}")
    for k in ("lines", "events", "cw_pos"):
        if req.get(k):
            bits.append(f"{_num(req[k])} {k.replace('_', ' ')}")
    if req.get("lookback_days"):
        bits.append(f"lookback {_num(req['lookback_days'])} d")
    if req.get("reason"):
        bits.append(f"reason: {req['reason']}")
    for k, v in res.items():
        if k in ("ok", "dry_run") or (k.endswith("status") and v in ("ok", req.get("status"))):
            continue
        if v is None or v is False or v == "" or (v == 0 and k != "onhand_removed_cs"):
            continue
        if k in _RESULT_TEXT:
            try:
                bits.append(_RESULT_TEXT[k](v))
            except (TypeError, ValueError):
                bits.append(f"{k.replace('_', ' ')}: {v}")
            continue
        label = k.replace("report_", "").replace("events_", "").replace("_", " ")
        bits.append(f"{label}: {_num(v) if isinstance(v, (int, float)) else v}")
    return bits


def build_activity(days: int = 14, include_burn: bool = False, limit: int = 400,
                   warehouse: str = "", distributor: str = "", po: str = "",
                   category: str = "", q: str = "") -> dict:
    from inventory_tracker import load_inventory, load_usage, load_inventory_audit
    from core import activity as act
    now = datetime.now()
    since = (now - timedelta(days=max(1, days))).isoformat(timespec="seconds")
    inv = load_inventory()
    by_name = {(it.get("name") or k).lower(): it for k, it in inv.items()}

    # PO -> (distributor, warehouse) for action-log rows, from the ledger.
    po_meta: dict = {}
    try:
        from blueprints.pos import build_po_ledger
        for r in build_po_ledger():
            po_meta[_norm_po_key(r.get("po_number") or "")] = (
                r.get("distributor") or "", r.get("warehouse") or "")
    except Exception as exc:  # noqa: BLE001 -- enrichment only
        _log_exc(exc, "activity: ledger")

    groups: dict = {}
    _last_key: list = []

    def _add(ts, cat, label, dist, wh, po_num, name, delta, detail, actor, src,
             extra=None):
        _last_key[:] = []
        ts = _norm_ts(ts)
        if ts < since:
            return
        gk = (cat, label, wh, _norm_po_key(po_num), ts[:16], src)
        _last_key.append(gk)
        g = groups.get(gk)
        if g is None:
            g = groups[gk] = {"ts": ts, "category": cat, "label": label,
                              "distributor": dist, "warehouse": wh,
                              "po_number": po_num, "delta_cs": 0.0, "items": [],
                              "detail": detail, "actor": actor, "source": src}
            if extra:
                g.update(extra)
        if ts > g["ts"]:
            g["ts"] = ts
        if name or delta:
            g["items"].append({"name": name, "delta": round(float(delta or 0), 2)})
            g["delta_cs"] += float(delta or 0)

    # 1) what scans applied
    for a in load_inventory_audit(0):
        et = a.get("event_type") or ""
        cat, label = _AUDIT_KINDS.get(et, ("correction", et.replace("_", " ").capitalize()))
        it = by_name.get((a.get("name") or "").lower()) or {}
        delta = a.get("delta") or 0
        if et == "usage_rate":
            delta = 0
        src = a.get("source") or ""
        detail = f"email: {a['subject']}" if a.get("subject") else ""
        _add(a.get("ts") or "", cat, label, it.get("distributor") or "",
             a.get("warehouse") or it.get("warehouse") or "", a.get("po_number") or "",
             a.get("name") or "", delta, detail,
             "mailbox scan" if src in ("ms365", "cowork-routine/graph-direct") else src,
             src)
        g = groups.get(_last_key[0]) if _last_key else None
        if g is not None and g["items"]:
            last = g["items"][-1]
            if et in ("usage_rate", "on_hand"):
                last["from"], last["to"] = a.get("old_quantity"), a.get("new_quantity")
            if a.get("on_order_delta") not in (None, 0):
                last["on_order"] = a.get("on_order_delta")
                g["on_order_cs"] = round(g.get("on_order_cs", 0.0)
                                         + float(a["on_order_delta"] or 0), 2)

    # 2) what moved on-hand
    for e in load_usage():
        kind = _usage_kind(e)
        if kind is None:
            continue
        cat, label = kind
        if cat == "burn" and not include_burn:
            continue
        it = inv.get(e.get("item_key") or "") or {}
        amt = float(e.get("amount") or 0)
        delta = -amt                          # usage convention: negative = added
        absorbed = (e.get("source") or "") == "on_order_absorbed"
        if absorbed:
            delta = 0.0
        if e.get("reversed") and (e.get("source") or "") == "on_order_rollover":
            label = label + " (later moved back on order)"
        wh = e.get("warehouse") or it.get("warehouse") or ""
        detail = (e.get("note") or "") if cat != "burn" else "weekly usage \u00f7 7, every SKU"
        _add(e.get("timestamp") or "", cat, label, it.get("distributor") or "", wh,
             e.get("po_number") or "", e.get("item_name") or it.get("name") or "",
             delta, detail, "system", e.get("source") or "")
        g = groups.get(_last_key[0]) if _last_key else None
        if absorbed and g is not None and g["items"]:
            q_abs = absorbed_row_qty(e)
            g["items"][-1]["absorbed"] = q_abs
            g["absorbed_cs"] = round(g.get("absorbed_cs", 0.0) + q_abs, 2)

    # 3) every change request
    for a in act.load(limit=0, since=since):
        pk = _norm_po_key(a.get("po_number") or "")
        dist, wh = po_meta.get(pk, ("", ""))
        req = a.get("request") or {}
        res = a.get("result") or {}
        bits = _action_bits(req, res)
        ok = (a.get("status") or 200) < 400 and res.get("ok", True) is not False
        delta = 0.0
        if isinstance(res.get("onhand_delta_cs"), (int, float)):
            delta = float(res["onhand_delta_cs"])
        elif isinstance(res.get("onhand_removed_cs"), (int, float)):
            delta = -float(res["onhand_removed_cs"])
        elif isinstance(res.get("removed_cs"), (int, float)):
            delta = -float(res["removed_cs"])
        _add(a.get("ts") or "", "action", (a.get("label") or a.get("path") or "Request")
             + ("" if ok else " (failed)"), dist, wh, a.get("po_number") or "", "",
             0.0, "; ".join(bits), a.get("actor") or "", a.get("endpoint") or "",
             {"delta_cs_action": round(delta, 2), "failed": not ok,
              "http_status": a.get("status")})

    out = []
    wh_f, dist_f = warehouse.strip().lower(), distributor.strip().lower()
    po_f, q_f, cat_f = _norm_po_key(po), q.strip().lower(), category.strip().lower()
    for g in groups.values():
        if wh_f and (g["warehouse"] or "").lower() != wh_f:
            continue
        if dist_f and (g["distributor"] or "").lower() != dist_f:
            continue
        if po_f and po_f not in _norm_po_key(g["po_number"]):
            continue
        if cat_f and g["category"] != cat_f:
            continue
        if g["category"] == "action" and g.get("delta_cs_action"):
            g["delta_cs"] = g["delta_cs_action"]
        g.pop("delta_cs_action", None)
        g["delta_cs"] = round(g["delta_cs"], 2)
        g["item_count"] = len(g["items"])
        if q_f and q_f not in " ".join(str(v) for v in (
                g["label"], g["warehouse"], g["po_number"], g["detail"], g["actor"],
                " ".join(i["name"] for i in g["items"]))).lower():
            continue
        out.append(g)
    out.sort(key=lambda g: g["ts"], reverse=True)
    counts: dict = {}
    for g in out:
        counts[g["category"]] = counts.get(g["category"], 0) + 1
    off = now.astimezone().utcoffset()
    return {"ok": True, "since": since, "days": days, "total": len(out),
            "counts": counts, "events": out[:limit],
            # server stamps are naive local; the page converts with this
            "tz_offset_min": int(off.total_seconds() // 60) if off else 0}


@activity_bp.route("/api/activity")
def api_activity():
    """Merged change timeline. Query: days (1-180, default 14), warehouse,
    distributor, po, category (count|po|arrival|action|usage|correction|burn),
    q (free text), include_burn=1, limit (default 400, max 3000)."""
    a = request.args
    try:
        days = max(1, min(int(a.get("days") or 14), 180))
    except ValueError:
        days = 14
    try:
        limit = max(1, min(int(a.get("limit") or 400), 3000))
    except ValueError:
        limit = 400
    try:
        data = build_activity(days=days,
                              include_burn=(a.get("include_burn") or "") in ("1", "true", "yes")
                              or (a.get("category") or "") == "burn",
                              limit=limit, warehouse=a.get("warehouse") or "",
                              distributor=a.get("distributor") or "",
                              po=a.get("po") or "", category=a.get("category") or "",
                              q=a.get("q") or "")
        if (a.get("flow") or "") in ("1", "true", "yes"):
            data["flow"] = po_flow_check()
        return jsonify(data)
    except Exception as exc:  # noqa: BLE001
        _log_exc(exc, "activity")
        return jsonify({"ok": False, "error": "internal error"}), 500
