"""PO health: the problems with purchase orders a person has to look at.

Two entry points:

* po_integrity_gate(...)  -- called by the email scan for every parsed PO
  PDF; a PO whose lines do not add up to what its PDF prints is NOT booked
  and is raised as issue ``po-total:<po>``.
* run_checks()            -- called by the hourly watchdog. Re-derives the
  periodic problems (stuck POs, untracked POs, a silent Cheney feed, usage
  that disagrees with invoices) and raises / clears their issues in
  core.feed_health. Takes no arguments, never raises, finishes in seconds
  (it only reads the JSON state files -- no network).

Issue keys (stable; the watchdog emails each new key once):
  po-total:<po>          parsed lines != printed total / unreadable rows
  po-stuck:<po>          a held PO nobody has confirmed for too long
  po-untracked:<po>      a PO seen with Lineage / on a freight invoice / on a
                         production sheet that the tracker never booked
  po-chase:<po>          follow-ups exhausted; a person must call
  po-hold-unreadable     the Lineage tracking file could not be read
  cheney-feed-stale      no Cheney file for 36 h (only once the feed is live)
  usage-diverge:<wh>     invoiced cases disagree with the reported usage
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone


def _fh():
    from core import feed_health
    return feed_health


def _log(msg: str) -> None:
    print(f"[po_health] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Scan-time gate
# ---------------------------------------------------------------------------

def po_integrity_gate(distributor: str, po, subject: str = "") -> str:
    """"" when the parsed PO may be booked; otherwise the scan-error text
    (and issue ``po-total:<po>`` is raised). ``po`` is a parser result with
    ``po_number`` and ``integrity_error``. Never raises."""
    try:
        err = str(getattr(po, "integrity_error", "") or "")
        if not err:
            return ""
        po_no = str(getattr(po, "po_number", "") or "").strip()
        msg = (f"{distributor} PO {po_no or '?'} ({subject!r}): NOT BOOKED -- "
               f"{err}. Nothing was added to Pending POs from this PDF; check "
               f"it by hand and fix the parser or enter the PO manually.")
        _fh().raise_issue(f"po-total:{po_no or subject or '?'}", msg, "error")
        return msg
    except Exception as exc:  # noqa: BLE001
        _log(f"integrity gate failed: {type(exc).__name__}")
        return ""


def flag_cw_review(po_no: str, errors: list) -> None:
    """A Chefs Warehouse PO record that does not reconcile (needs_review)."""
    try:
        _fh().raise_issue(
            f"po-total:{po_no}",
            f"Chefs Warehouse PO {po_no}: needs review -- " + "; ".join(errors)
            + ". The record is stored as read; check it against the PDF.",
            "warn")
    except Exception as exc:  # noqa: BLE001
        _log(f"flag_cw_review failed: {type(exc).__name__}")


def _age_out(prefix: str, hours: float, now: datetime) -> None:
    """Clear ``prefix`` issues nobody has re-raised for ``hours`` (the scan
    re-raises a live problem every 6 h; one that stopped is resolved)."""
    fh = _fh()
    for key, iss in (fh.snapshot().get("issues") or {}).items():
        if not key.startswith(prefix):
            continue
        seen = _parse(iss.get("last_seen") or "")
        if seen and now - seen > timedelta(hours=hours):
            fh.clear_issue(key)


def _parse(raw: str):
    raw = str(raw or "").strip()
    if not raw:
        return None
    if raw[-1:] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None \
        else dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Held / stuck POs and "the count already contains it" (PO ledger rows)
# ---------------------------------------------------------------------------
STUCK_APPT_PASSED_DAYS = 3        # appointment passed, no delivery confirmation
STUCK_REQUESTED_DAYS = 14         # sent to Lineage, never scheduled
STUCK_NO_TRIGGER_DAYS = 30        # pending with no date that could ever fire
STUCK_REBOOKED_DAYS = 7           # re-booked after a count absorbed it
STUCK_PAST_DATE_DAYS = 14         # its date passed but something still holds it
ABSORB_MIN_HOURS_AFTER = 48       # the count must be this long after the PO was due
ABSORB_SLACK_PCT = 0.10
ABSORB_SLACK_MIN_CS = 8.0
ABSORB_ENDPOINT = "/api/pos/absorb-pending"


def _aware(raw, warehouse: str = ""):
    """ISO stamp -> aware UTC. A stamp with no offset is the dock's wall
    clock (appointment / arrival dates) when ``warehouse`` is given, else
    server-local."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    t = raw[:-1] + "+00:00" if raw[-1:] in ("Z", "z") else raw
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        try:
            dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if not 2020 <= dt.year <= 2100:
        return None
    if dt.tzinfo is None:
        if warehouse:
            from zoneinfo import ZoneInfo
            from integrations.po_delivery import dock_zone
            dt = dt.replace(tzinfo=ZoneInfo(dock_zone(warehouse)))
        else:
            dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def _days(since, now) -> int:
    return max(0, int((now - since).total_seconds() // 86400))


def hold_state(row: dict, trk, now: datetime, pending: bool = True) -> dict:
    """{held_days, hold_kind, stuck, stuck_reason} for one PO-ledger row.

    ``row`` is a build_po_ledger record (status already set), ``trk`` its
    Lineage tracking record or None, ``now`` aware UTC. A PO that is arrived
    or canceled is never held. ``held_days`` counts whole days since the
    thing it is waiting on was due.
    """
    out = {"held_days": 0, "hold_kind": "", "stuck": False, "stuck_reason": ""}
    if not pending or (row.get("status") or "") in ("arrived", "canceled", "cancelled"):
        return out
    from integrations.po_delivery import HOLD_STATUSES
    wh = row.get("warehouse") or ""
    st = (trk or {}).get("status") or ""
    po = row.get("po_number") or ""

    def _set(kind, since, limit, why):
        out["hold_kind"] = kind
        if since is None or since > now:
            return
        out["held_days"] = _days(since, now)
        if now - since > timedelta(days=limit):
            out["stuck"] = True
            out["stuck_reason"] = why.format(d=out["held_days"])

    if st == "scheduled" and _aware(trk.get("delivery_appt")):
        _set("awaiting_delivery_confirmation", _aware(trk.get("delivery_appt")),
             STUCK_APPT_PASSED_DAYS,
             f"PO {po}: Lineage's delivery appointment passed {{d}} days ago and "
             "nobody has confirmed the delivery -- it is still Pending and NOT "
             "in on-hand")
    elif st in HOLD_STATUSES:
        _set("awaiting_appointment", _aware(trk.get("requested_at")),
             STUCK_REQUESTED_DAYS,
             f"PO {po}: sent to Lineage {{d}} days ago, still no delivery "
             "appointment or confirmation")
    elif row.get("rebooked_after_count"):
        _set("rebooked_after_count", _aware(row.get("rebooked_after_count"), wh),
             STUCK_REBOOKED_DAYS,
             f"PO {po}: re-booked {{d}} days ago after a count had already "
             "absorbed its delivery; it waits for a ship / arrival date or to "
             "be removed")
    elif not (row.get("arrival_date") or row.get("eta")):
        _set("no_trigger_date", _aware(row.get("ordered_at"), wh),
             STUCK_NO_TRIGGER_DAYS,
             f"PO {po}: pending {{d}} days with no ship, arrival or ETA date -- "
             "nothing will ever move it")
    else:
        _set("past_date", _aware(row.get("arrival_date") or row.get("eta"), wh),
             STUCK_PAST_DATE_DAYS,
             f"PO {po}: its arrival date passed {{d}} days ago but it is still "
             "held as Pending")
    return out


def suggest_absorb(row: dict, inv: dict, trk, now: datetime):
    """When the last count clearly already CONTAINS a still-pending PO:
    {count_at, counted, projected_without, projected_with, receipt, slack,
     arrived_on, reason, endpoint, payload, lines}, else None.

    The count must have been taken >= 48 h after the PO was due (Lineage's
    appointment, else its expected arrival), every pending line must be on
    that same count with a recorded projection (item["count_check"]), and the
    counted total must reach projected-on-hand + the PO - slack
    (slack = max(10% of the PO, 8 cs)). Never applied automatically:
    ``endpoint`` + ``payload`` are what a one-click button posts.
    """
    if (row.get("status") or "") in ("arrived", "canceled", "cancelled"):
        return None
    wh = row.get("warehouse") or ""
    due = None
    if trk and (trk.get("status") or "") == "scheduled":
        due = _aware(trk.get("delivery_appt"))
    elif not trk or (trk.get("status") or "") in ("released", ""):
        due = _aware(row.get("arrival_date") or row.get("eta"), wh)
    if due is None or due > now:
        return None
    lines, count_ats = [], set()
    for ln in row.get("lines") or []:
        if ln.get("arrived") or ln.get("absorbed") or not ln.get("item_key"):
            continue
        item = (inv or {}).get(ln["item_key"])
        if item is None:
            return None
        count_at = item.get("last_count_at") or ""
        cdt = _aware(count_at, wh)
        chk = item.get("count_check") or {}
        if (cdt is None or cdt - due < timedelta(hours=ABSORB_MIN_HOURS_AFTER)
                or item.get("last_count_qty") is None
                or chk.get("count_at") != count_at or chk.get("projected") is None):
            return None
        count_ats.add(count_at)
        lines.append({"item_key": ln["item_key"], "variety": ln.get("variety") or "",
                      "receipt": float(ln.get("qty") or 0),
                      "counted": float(item.get("last_count_qty") or 0),
                      "projected_without": float(chk.get("projected") or 0)})
    if not lines or len(count_ats) != 1:
        return None
    receipt = sum(l["receipt"] for l in lines)
    counted = sum(l["counted"] for l in lines)
    without = sum(l["projected_without"] for l in lines)
    slack = max(ABSORB_SLACK_PCT * receipt, ABSORB_SLACK_MIN_CS)
    if receipt <= 0 or counted < without + receipt - slack:
        return None
    count_at = next(iter(count_ats))
    from core.util import dock_local_day
    from integrations.po_delivery import dock_zone
    from zoneinfo import ZoneInfo
    arrived_on = due.astimezone(ZoneInfo(dock_zone(wh))).strftime("%Y-%m-%d")
    count_day = dock_local_day(count_at, wh)
    po = row.get("po_number") or ""
    reason = (f"the {count_day} count shows {counted:g} cs across this PO's "
              f"SKUs; without the PO we projected {without:g} cs, with it "
              f"{without + receipt:g} cs -- the count already contains the "
              f"{receipt:g} cs that were due {arrived_on}")
    return {
        "count_at": count_at, "counted": round(counted, 2),
        "projected_without": round(without, 2),
        "projected_with": round(without + receipt, 2),
        "receipt": round(receipt, 2), "slack": round(slack, 2),
        "arrived_on": arrived_on, "reason": reason,
        "endpoint": ABSORB_ENDPOINT, "method": "POST",
        "payload": {"po_number": po, "count_at": count_at,
                    "arrived_on": arrived_on, "dry_run": False,
                    "reason": f"count {count_day} already contains the PO "
                              f"(counted {counted:g} cs vs {without:g} cs "
                              f"projected without it)"},
        "lines": lines,
    }


# ---------------------------------------------------------------------------
# Periodic checks
# ---------------------------------------------------------------------------

def _check_stuck(now: datetime) -> dict:
    """po-stuck:<po> for every ledger row that is stuck (see hold_state)."""
    from blueprints.pos import build_po_ledger
    fh = _fh()
    keep, n_suggest = set(), 0
    for r in build_po_ledger():
        if not r.get("stuck"):
            continue
        key = f"po-stuck:{r.get('po_number')}"
        keep.add(key)
        msg = r.get("stuck_reason") or f"PO {r.get('po_number')} is stuck"
        msg += (f" ({r.get('distributor') or '?'} {r.get('warehouse') or ''}, "
                f"{r.get('total_cs') or 0:g} cs).")
        if r.get("suggest_absorb"):
            n_suggest += 1
            msg += (" The last count appears to already contain it: "
                    + r["suggest_absorb"]["reason"]
                    + ". Use 'Absorb into count' on the Pending POs tab.")
        fh.raise_issue(key, msg, "warn")
    fh.clear_issues_with_prefix("po-stuck:", keep=keep)
    return {"stuck": len(keep), "suggest_absorb": n_suggest}


def _check_hold_integrity(now: datetime) -> dict:
    """po-hold-unreadable: POs under a Lineage hold whose tracking record
    can't be read. The rollover keeps them pending (it used to roll them
    into stock on their date); a person has to restore the file."""
    import inventory_tracker as it
    from integrations.po_delivery import norm_po
    fh = _fh()
    try:
        tracking = it.load_po_delivery()
        if not isinstance(tracking, dict):
            raise TypeError("not an object")
        why = ""
    except Exception as exc:  # noqa: BLE001
        tracking, why = None, f"{type(exc).__name__}"
    orphans = set()
    for item in it._read_json(it.INVENTORY_FILE, {}).values():
        for e in (item.get("on_order") or []):
            if ((e.get("arrival_source") or "") == "operator"
                    and e.get("arrival_confirmed")):
                continue
            flagged = e.get("lineage_hold") or str(
                e.get("arrival_source") or "").startswith("lineage")
            if flagged and (tracking is None
                            or norm_po(e.get("po_number") or "") not in tracking):
                orphans.add(e.get("po_number") or "?")
    if tracking is None or orphans:
        fh.raise_issue(
            "po-hold-unreadable",
            ("The Lineage tracking file (po_delivery.json) could not be read "
             f"({why}). " if tracking is None else
             "Lineage tracking has no record for PO(s) that are under a "
             "Lineage hold. ")
            + (f"Kept pending, NOT rolled into on-hand: {', '.join(sorted(orphans))}. "
               if orphans else "")
            + "Restore data/po_delivery.json from a backup, or confirm each "
              "PO's arrival by hand on the Pending POs tab.", "error")
    else:
        fh.clear_issue("po-hold-unreadable")
    return {"tracking_readable": tracking is not None, "orphans": sorted(orphans)}



TRACKED_DISTRIBUTORS = ("us foods", "usf", "cheney", "chefs", "chef's")
UNTRACKED_LOOKBACK_DAYS = 60
ARRIVED_UNCONFIRMED_DAYS = 30


def _is_tracked_distributor(*texts) -> bool:
    blob = " ".join(str(t or "") for t in texts).lower()
    return any(d in blob for d in TRACKED_DISTRIBUTORS)


def _recent(raw, now: datetime, days: int) -> bool:
    dt = _parse(str(raw or "")[:25]) or _parse(str(raw or "")[:10])
    return dt is not None and now - dt <= timedelta(days=days)


def untracked_pos(now: datetime) -> dict:
    """{po_key: {"po_number", "seen": [where...]}} -- PO numbers that exist
    somewhere real (a Quote and Schedule request to Lineage, a Lineage freight
    invoice, a Daily Production sheet) for a distributor the tracker books,
    but that the PO ledger has never heard of. The lost-PO detector: an
    emailed PO that failed to parse leaves no other trace."""
    import inventory_tracker as it
    from blueprints.pos import build_po_ledger
    from core.util import _norm_po_key
    known = {_norm_po_key(r.get("po_number") or "") for r in build_po_ledger()}
    try:
        known |= {_norm_po_key(k) for k in (it.load_canceled_pos() or {})}
    except Exception:  # noqa: BLE001
        pass
    out: dict = {}

    def _add(po, where):
        key = _norm_po_key(po)
        if not key or key in known or not any(ch.isdigit() for ch in key):
            return
        out.setdefault(key, {"po_number": str(po).strip(), "seen": []})["seen"].append(where)

    try:
        cache = it.load_po_delivery_threads() or {}
    except Exception:  # noqa: BLE001 -- a mailbox mirror; skip it
        cache = {}
    for u in cache.get("not_in_tracker") or []:
        if (_is_tracked_distributor(u.get("address"))
                and _recent(u.get("request_sent"), now, UNTRACKED_LOOKBACK_DAYS)):
            _add(u.get("po_number"),
                 f"sent to Lineage {str(u.get('request_sent') or '')[:10]} "
                 f"({u.get('subject') or 'Quote and Schedule'}; "
                 f"{u.get('quantity') or '?'} to {u.get('address') or '?'})")
    try:
        invoices = it.load_freight_invoices() or []
    except Exception:  # noqa: BLE001
        invoices = []
    for f in invoices:
        if (_is_tracked_distributor(f.get("distributor"), f.get("consignee_name"))
                and _recent(f.get("ship_date") or f.get("invoice_date"), now,
                            UNTRACKED_LOOKBACK_DAYS)):
            _add(f.get("po_number"),
                 f"Lineage freight invoice {f.get('invoice_number') or '?'} "
                 f"shipped {str(f.get('ship_date') or '')[:10] or '?'} to "
                 f"{f.get('dest_dc') or f.get('consignee_name') or '?'}")
    try:
        production = it._read_json(it.PRODUCTION_FILE, [])       # read-only
    except Exception:  # noqa: BLE001
        production = []
    recs = production.get("records") if isinstance(production, dict) else production
    for r in recs or []:
        if not isinstance(r, dict) or r.get("parse_error"):
            continue
        if (_is_tracked_distributor(r.get("distributor"))
                and _recent(r.get("production_date"), now, UNTRACKED_LOOKBACK_DAYS)):
            _add(r.get("po_number"),
                 f"Daily Production {str(r.get('production_date') or '')[:10]} "
                 f"({r.get('total_cases') or '?'} cs for {r.get('warehouse') or '?'})")
    return out


def _check_untracked(now: datetime) -> dict:
    fh = _fh()
    found = untracked_pos(now)
    keep = set()
    for key, u in found.items():
        k = f"po-untracked:{key}"
        keep.add(k)
        fh.raise_issue(
            k, f"PO {u['po_number']} is NOT in the tracker but was "
               + "; ".join(u["seen"][:3])
               + ". Its PO email was probably never received or did not parse -- "
                 "find the PO and enter it, or the stock it delivers will never "
                 "be counted as on order.", "error")
    fh.clear_issues_with_prefix("po-untracked:", keep=keep)
    return {"untracked": sorted(found)}


def _check_arrived_unconfirmed(now: datetime) -> dict:
    """po-arrived-unconfirmed:<po>: requested from Lineage in the last 30
    days, never confirmed delivered, yet the ledger already says arrived (it
    rolled in on a date, or was marked / absorbed by hand)."""
    import inventory_tracker as it
    fh = _fh()
    try:
        cache = it.load_po_delivery_threads() or {}
    except Exception:  # noqa: BLE001
        cache = {}
    keep = set()
    for u in cache.get("requested_but_arrived") or []:
        if not _recent(u.get("request_sent"), now, ARRIVED_UNCONFIRMED_DAYS):
            continue
        k = f"po-arrived-unconfirmed:{u.get('po_number')}"
        keep.add(k)
        fh.raise_issue(
            k, f"PO {u.get('po_number')} was sent to Lineage "
               f"{str(u.get('request_sent') or '')[:10]} and never confirmed "
               f"delivered, but the tracker already shows it ARRIVED "
               f"({u.get('arrival_kind') or 'by date'} "
               f"{str(u.get('arrival_date') or '')[:10]}). If it has not really "
               "arrived, Reopen it on the Pending POs tab.", "warn")
    fh.clear_issues_with_prefix("po-arrived-unconfirmed:", keep=keep)
    return {"arrived_unconfirmed": sorted(k.split(":", 1)[1] for k in keep)}


def _check_chase(now: datetime) -> dict:
    """po-chase:<po>: automatic follow-ups are exhausted -- a person calls."""
    import inventory_tracker as it
    from blueprints.delivery import _is_open, _ledger_by_po
    from integrations.po_delivery import chase_exhausted
    fh = _fh()
    tracking = it.load_po_delivery() or {}
    ledger = _ledger_by_po()
    keep = set()
    for key, rec in tracking.items():
        if not _is_open(ledger.get(key), rec):
            continue
        why = chase_exhausted(rec, now)
        if not why:
            continue
        k = f"po-chase:{rec.get('po_number') or key}"
        keep.add(k)
        lr = ledger.get(key) or {}
        fh.raise_issue(
            k, f"PO {rec.get('po_number') or key} ({lr.get('distributor') or '?'} "
               f"{lr.get('warehouse') or ''}): {why}. Automatic follow-ups to "
               "Lineage have STOPPED -- call Lineage or the DC, then mark it "
               "Arrived (with the date) or Reopen / remove it.", "warn")
    fh.clear_issues_with_prefix("po-chase:", keep=keep)
    return {"needs_human": sorted(k.split(":", 1)[1] for k in keep)}


def _check_parse_issues(now: datetime) -> dict:
    _age_out("po-total:", 48, now)
    return {}


CHECKS = [("parse", _check_parse_issues), ("stuck", _check_stuck),
          ("hold", _check_hold_integrity), ("untracked", _check_untracked),
          ("arrived_unconfirmed", _check_arrived_unconfirmed),
          ("chase", _check_chase)]


def run_checks() -> dict:
    """Run every periodic PO check. Returns {check: summary | {"error": ..}}.
    Never raises."""
    out: dict = {}
    now = datetime.now(timezone.utc)
    for name, fn in list(CHECKS):
        try:
            out[name] = fn(now) or {}
        except Exception as exc:  # noqa: BLE001
            _log(f"check {name} failed: {type(exc).__name__}: {exc}")
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return out
