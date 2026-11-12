"""Periodic ingest checks -- things no single scan can see.

``run_checks()`` takes no arguments and NEVER raises; the hourly watchdog
calls it and then reports whatever is new in core.feed_health. Each check
raises a stable-keyed issue when something is wrong and clears it when it is
not, so a condition alerts once and closes itself.

Checks
------
po-gap:<warehouse>
    A warehouse that normally sends a PO every N days has sent none for more
    than twice that (and more than two weeks). A PO that never reached the
    mailbox -- or one the scanner cannot see -- produces no error anywhere;
    the only trace is the silence.

on-order-mismatch:<warehouse>
    The distributor's own weekly report says how many cases it has ON ORDER
    per SKU. That column is parsed and kept (item["reported_on_order"], never
    applied). When it differs from what we hold as pending for the same SKU
    by more than a pallet, either a PO is missing here or one we still show
    as pending has already been received there.

stale issues
    Issues raised by the apply path (a rejected on-hand, a held report) have
    no scan that re-derives them, so they are closed here once the warehouse
    has counted again or they have not been seen for STALE_ISSUE_DAYS.
"""
from __future__ import annotations

import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

# --- PO cadence ---------------------------------------------------------------
PO_GAP_MIN_GAPS = 4        # gaps needed before a cadence is trusted (5 PO days)
PO_GAP_MAX_GAPS = 8        # ...taken from the most recent this-many gaps
PO_GAP_FACTOR = 2.0        # overdue when silence > factor x the median gap
PO_GAP_MIN_DAYS = 14       # ...and longer than this, whatever the cadence

# --- on-order cross-check -----------------------------------------------------
PALLET_CS = 56             # one pallet; smaller differences are timing noise
ON_ORDER_REPORT_MAX_AGE_DAYS = 9   # ignore a reported figure older than this

# --- stale issues -------------------------------------------------------------
STALE_ISSUE_DAYS = 15
_APPLY_ISSUE_PREFIXES = ("onhand-rejected:", "report-held:")


def _log(exc, ctx):
    print(f"[ingest_health {ctx}] {type(exc).__name__}: {exc}", file=sys.stderr)


def _off(name: str) -> bool:
    return os.environ.get(name, "on").strip().lower() in ("off", "0", "false", "no")


def _dt(value):
    from inventory_tracker import _parse_iso_utc
    return _parse_iso_utc(str(value or ""))


# ------------------------------------------------------------------------------
# PO cadence
# ------------------------------------------------------------------------------

def po_days_by_warehouse(ledger: list) -> dict:
    """{(distributor, warehouse): sorted distinct PO dates} from the canonical
    PO ledger (blueprints.pos.build_po_ledger): every PO document on file --
    pending, in transit, arrived, Chefs Warehouse. A PO is dated by when it
    was ordered; an old arrived row without that falls back to its arrival.
    Canceled POs are left out."""
    out: dict = {}
    for r in ledger or []:
        if (r.get("status") or "") == "canceled" or r.get("_canceled"):
            continue
        wh = (r.get("warehouse") or "").strip()
        if not wh:
            continue
        d = _dt(r.get("ordered_at")) or _dt(r.get("arrival_date"))
        if d is None:
            continue
        out.setdefault(((r.get("distributor") or "").strip(), wh), set()).add(d.date())
    return {k: sorted(v) for k, v in out.items()}


def po_gap_verdict(days: list, today) -> dict:
    """Is this warehouse overdue for a PO? ``days`` = sorted distinct dates."""
    if len(days) < PO_GAP_MIN_GAPS + 1:
        return {"overdue": False, "reason": "not enough history",
                "po_days": len(days)}
    gaps = [(b - a).days for a, b in zip(days, days[1:])][-PO_GAP_MAX_GAPS:]
    median = float(statistics.median(gaps))
    since = (today - days[-1]).days
    overdue = since > PO_GAP_FACTOR * median and since > PO_GAP_MIN_DAYS
    return {"overdue": overdue, "median_gap_days": median, "days_since_last": since,
            "last_po_date": days[-1].isoformat(), "gaps_used": len(gaps)}


def check_po_cadence(now=None) -> dict:
    from core import feed_health
    from blueprints.pos import build_po_ledger
    today = (now or datetime.now(timezone.utc)).date()
    # Warehouses to leave out (a DC we stopped supplying): PO_GAP_IGNORE,
    # separated by ";" because the names contain commas ("Alcoa, TN; ...").
    ignore = {w.strip().lower() for w in
              os.environ.get("PO_GAP_IGNORE", "").split(";") if w.strip()}
    out = {}
    for (dist, wh), days in sorted(po_days_by_warehouse(build_po_ledger()).items()):
        key = f"po-gap:{wh}"
        if wh.lower() in ignore:
            feed_health.clear_issue(key)
            continue
        v = po_gap_verdict(days, today)
        out[wh] = v
        if v["overdue"]:
            feed_health.raise_issue(
                key,
                f"{wh} ({dist}): no purchase order for {v['days_since_last']} "
                f"days (last one {v['last_po_date']}); it normally orders "
                f"about every {v['median_gap_days']:g} days. Check the "
                "mailbox for a PO the scan did not read, or confirm with the "
                "buyer that none was placed.", "warn")
        else:
            feed_health.clear_issue(key)
    return out


# ------------------------------------------------------------------------------
# On-order cross-check
# ------------------------------------------------------------------------------

def _pending_as_of(item: dict, key: str, usage: list, as_of) -> float:
    """Cases we held as ON ORDER for this SKU at ``as_of``: pending rows that
    were already booked then, plus receipts that have rolled into on-hand
    SINCE (they were still on order at the time of the report)."""
    total = 0.0
    for o in (item.get("on_order") or []):
        booked = _dt(o.get("source_received_at")) or _dt(o.get("ordered_at"))
        if booked is not None and booked > as_of:
            continue
        total += float(o.get("qty") or 0)
    from core.util import rollover_row_live
    for row in usage:
        if row.get("item_key") != key or not rollover_row_live(row):
            continue
        arrived = _dt(row.get("arrival_date")) or _dt(row.get("timestamp"))
        if arrived is not None and arrived > as_of:
            total += abs(float(row.get("amount") or 0))
    return total


def check_on_order(now=None) -> dict:
    from core import feed_health
    from inventory_tracker import load_inventory, load_usage
    now = now or datetime.now(timezone.utc)
    inv = load_inventory()
    usage = None
    by_wh: dict = {}
    seen_wh = set()
    for key, item in inv.items():
        rep = item.get("reported_on_order")
        if not isinstance(rep, dict) or rep.get("qty") is None:
            continue
        wh = item.get("warehouse") or ""
        at = _dt(rep.get("at"))
        if at is None or (now - at) > timedelta(days=ON_ORDER_REPORT_MAX_AGE_DAYS):
            continue
        seen_wh.add(wh)
        if usage is None:
            usage = load_usage() or []
        ours = _pending_as_of(item, key, usage, at)
        theirs = float(rep["qty"])
        if abs(theirs - ours) > PALLET_CS:
            by_wh.setdefault(wh, []).append({
                "sku": item.get("name") or key, "reported": theirs,
                "pending": round(ours, 2), "as_of": rep.get("at")})
    # Clear for every warehouse that has a figure and no mismatch; warehouses
    # with no fresh figure keep whatever state they had cleared by age below.
    for wh in sorted(seen_wh):
        key = f"on-order-mismatch:{wh}"
        rows = by_wh.get(wh)
        if not rows:
            feed_health.clear_issue(key)
            continue
        detail = "; ".join(
            f"{r['sku']}: report {r['reported']:g} cs vs pending {r['pending']:g} cs"
            for r in rows[:6])
        feed_health.raise_issue(
            key,
            f"{wh}: the distributor's report (as of {str(rows[0]['as_of'])[:10]}) "
            f"and our pending POs disagree by more than a pallet on "
            f"{len(rows)} SKU(s) -- {detail}. A PO may be missing here, or "
            "one shown as pending has already been received.", "warn")
    return by_wh


# ------------------------------------------------------------------------------
# Stale issues
# ------------------------------------------------------------------------------

def expire_stale_issues(now=None) -> list:
    from core import feed_health
    from inventory_tracker import warehouse_freshness
    now = now or datetime.now(timezone.utc)
    issues = feed_health.snapshot().get("issues") or {}
    latest = {}
    try:
        latest = {r.get("warehouse") or "": _dt(r.get("last_count_at"))
                  for r in warehouse_freshness()}
    except Exception as exc:  # noqa: BLE001
        _log(exc, "freshness")
    closed = []
    for key, rec in issues.items():
        if not key.startswith(_APPLY_ISSUE_PREFIXES + ("on-order-mismatch:",)):
            continue
        last_seen = _dt(rec.get("last_seen"))
        first_seen = _dt(rec.get("first_seen"))
        stale = last_seen is not None and (now - last_seen) > timedelta(days=STALE_ISSUE_DAYS)
        counted_since = False
        if key.startswith("report-held:"):
            wh = key.split(":", 2)[1]
            c = latest.get(wh)
            counted_since = bool(c and first_seen and c > first_seen)
        if stale or counted_since:
            feed_health.clear_issue(key)
            closed.append(key)
    return closed


# ------------------------------------------------------------------------------

def run_checks() -> dict:
    """Run every periodic ingest check. No arguments, never raises."""
    out: dict = {"ok": True, "checks": {}, "errors": []}
    steps = (
        ("po_cadence", "PO_GAP_CHECK", check_po_cadence),
        ("on_order", "ON_ORDER_CROSSCHECK", check_on_order),
        ("stale_issues", "", expire_stale_issues),
    )
    for name, switch, fn in steps:
        if switch and _off(switch):
            out["checks"][name] = "disabled"
            continue
        try:
            out["checks"][name] = fn()
        except Exception as exc:  # noqa: BLE001 -- one check never stops the rest
            _log(exc, name)
            out["ok"] = False
            out["errors"].append(f"{name}: {type(exc).__name__}")
    try:
        from core import feed_health
        feed_health.record("ingest-checks", out["ok"],
                           "; ".join(out["errors"]) or "ok")
    except Exception as exc:  # noqa: BLE001
        _log(exc, "record")
    return out


__all__ = ["run_checks", "check_po_cadence", "check_on_order",
           "expire_stale_issues", "po_gap_verdict", "po_days_by_warehouse"]
