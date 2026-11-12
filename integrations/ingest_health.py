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
        if not key.startswith(_APPLY_ISSUE_PREFIXES):
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


__all__ = ["run_checks", "check_po_cadence", "expire_stale_issues",
           "po_gap_verdict", "po_days_by_warehouse"]
