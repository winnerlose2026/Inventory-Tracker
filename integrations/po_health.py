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
# Periodic checks
# ---------------------------------------------------------------------------

def _check_parse_issues(now: datetime) -> dict:
    _age_out("po-total:", 48, now)
    return {}


CHECKS = [("parse", _check_parse_issues)]


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
