"""Watchdog: notices when a feed has stopped or the books stop adding up, and
tells a person once.

Runs hourly (scheduler.py -> POST /api/watchdog/run). Each run:

  1. works out which feeds are stale (feeds_view -- also what the UI banner
     reads through GET /api/feeds/health);
  2. calls the optional checks other modules provide
     (integrations.ingest_health.run_checks, integrations.po_health.run_checks);
  3. runs its own checks and opens / clears issues in core.feed_health;
  4. at the 07:xx UTC run, checks each SKU's on-hand against
     last count + receipts since - burns since;
  5. emails ONE message listing the issues nobody has been told about yet,
     and at the 13:xx UTC run a digest when anything is still open.
     Nothing is sent when everything is green.

Never raises: a monitoring failure is itself recorded (feed "watchdog").
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

import inventory_tracker as it
from core import feed_health

# Hours a feed may go without a successful run before it is "stale".
FEED_MAX_AGE_HOURS = {
    "mailbox-scan": 13,        # 6-hourly cron: two missed runs
    "production-scan": 26,
    "forecast-burn": 26,
    "graph-renew": 26,
    "delivery-sync": 1.5,      # only inside its 10:00-03:59 UTC window
    "toast-nightly": 26,
    "backup": 26,
    "backup-offsite": 30,
    "bakery-workbook": 240,    # 10 days
    "watchdog": 2,
    "scheduler": 0.25,         # 5-minute heartbeat
}
# Feeds that count as stale even when they have NEVER reported. Kept small so
# a first deploy is not a wall of red: everything else only goes stale after
# it has run at least once.
REQUIRED_FEEDS = ("mailbox-scan", "forecast-burn", "backup")

FRIENDLY = {
    "mailbox-scan": "The mailbox scan (reads PO and count emails)",
    "production-scan": "The Daily Production sheet scan",
    "forecast-burn": "The daily usage burn-down",
    "graph-renew": "The mailbox subscription renewal",
    "delivery-sync": "The Lineage delivery-confirmation check",
    "toast-nightly": "The nightly Toast sales pull",
    "backup": "The hourly backup",
    "backup-offsite": "The daily off-site copy of the backup",
    "bakery-workbook": "The Bakery Model workbook import",
    "watchdog": "The watchdog itself",
    "scheduler": "The in-app scheduler",
}

UNPARSED_MAX_AGE_HOURS = 6
WAREHOUSE_STALE_DAYS = 8
DISK_MIN_FREE_FRACTION = 0.20
BOOKS_TOLERANCE_CS = 0.5
BOOKS_MAX_ISSUES = 20
RENOTIFY_HOURS = 24
BOOKS_HOUR_UTC = 7
DIGEST_HOUR_UTC = 13


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str):
    """ISO string -> aware UTC datetime. A naive value is LOCAL server time
    (scan_health.json is written with datetime.now())."""
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc)


def _hash(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:10]


def _app_url() -> str:
    return (os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL")
            or "https://bagel-inventory.onrender.com").rstrip("/")


# ---------------------------------------------------------------------------
# Feed view (GET /api/feeds/health)
# ---------------------------------------------------------------------------

def _mailbox_scan_from_scan_health(existing: dict | None):
    """The 6-hourly scan has always written data/scan_health.json. Use it for
    the "mailbox-scan" feed whenever it is newer than what feed_health holds,
    so the feed is right even before the scan itself reports here."""
    try:
        rec = it.load_scan_health() or {}
    except Exception:  # noqa: BLE001
        return existing
    ts = _parse(rec.get("ts") or "")
    if ts is None:
        return existing
    cur_attempt = _parse((existing or {}).get("last_attempt") or "")
    if cur_attempt is not None and cur_attempt >= ts:
        return existing
    failed = bool(rec.get("mailbox_failures")) or (
        str(rec.get("status") or "").lower() in ("error", "failed"))
    out = dict(existing or {})
    out["last_attempt"] = _iso(ts)
    out["ok"] = not failed
    if not failed:
        out["last_ok"] = _iso(ts)
    out["detail"] = ("mailbox could not be read: %s"
                     % "; ".join(str(x) for x in rec.get("mailbox_failures")[:2])
                     if rec.get("mailbox_failures") else
                     "%s message(s) seen, %s parsed"
                     % (rec.get("messages_seen"), rec.get("messages_parsed")))[:500]
    return out


def _max_age(name: str, now: datetime):
    """Allowed hours since last_ok for `name` right now; None = no limit."""
    limit = FEED_MAX_AGE_HOURS.get(name)
    if limit is None:
        return None
    if name == "delivery-sync":
        # Runs every 20 min from 10:00 to 03:59 UTC and is idle 04:00-09:59.
        if 4 <= now.hour < 10:
            return None
        if now.hour == 10 or (now.hour == 11 and now.minute < 30):
            return limit + 6           # the idle gap has only just ended
    return limit


def feeds_view(now: datetime | None = None) -> dict:
    """The contract the UI banner and the off-box tasks read:

    {"ok", "generated_at", "feeds": {name: {"ok", "last_ok", "last_attempt",
     "detail", "stale", "max_age_hours"}}, "issues": [{"key", "message",
     "severity", "first_seen", "last_seen"}]}
    """
    now = now or _utcnow()
    snap = feed_health.snapshot()
    raw = dict(snap.get("feeds") or {})
    ms = _mailbox_scan_from_scan_health(raw.get("mailbox-scan"))
    if ms:
        raw["mailbox-scan"] = ms
    feeds = {}
    for name in sorted(set(raw) | set(REQUIRED_FEEDS)):
        rec = raw.get(name)
        limit = _max_age(name, now)
        if rec is None:                      # required, never reported
            feeds[name] = {"ok": False, "last_ok": "", "last_attempt": "",
                           "detail": "has not run yet", "stale": True,
                           "max_age_hours": FEED_MAX_AGE_HOURS.get(name)}
            continue
        last_ok = _parse(rec.get("last_ok") or "")
        stale = False
        if limit is not None:
            stale = last_ok is None or (now - last_ok) > timedelta(hours=limit)
        feeds[name] = {
            "ok": bool(rec.get("ok")),
            "last_ok": rec.get("last_ok") or "",
            "last_attempt": rec.get("last_attempt") or "",
            "detail": str(rec.get("detail") or ""),
            "stale": bool(stale),
            "max_age_hours": FEED_MAX_AGE_HOURS.get(name),
        }
    issues = [{"key": k, "message": v.get("message") or "",
               "severity": v.get("severity") or "warn",
               "first_seen": v.get("first_seen") or "",
               "last_seen": v.get("last_seen") or ""}
              for k, v in sorted((snap.get("issues") or {}).items(),
                                 key=lambda kv: kv[1].get("first_seen") or "")]
    ok = (not issues
          and not any(f["stale"] or not f["ok"] for f in feeds.values()))
    return {"ok": ok, "generated_at": _iso(now), "feeds": feeds,
            "issues": issues}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _when(ts: str) -> str:
    dt = _parse(ts)
    return dt.strftime("%b %d %H:%M UTC") if dt else "never"


# A feed whose last run failed is only worth an alert once it has gone this
# long without a success: the 20-minute jobs hit the odd busy lock or Graph
# timeout and recover on the next run.
FAILING_GRACE_HOURS = 3


# Feeds whose job raises its own issue for the same failure; the generic
# stale/failing alert would only say it again (the 2026-11-13 digest listed
# one Microsoft 503 three times).
_OWN_ISSUE_FEEDS = {
    "graph-renew": "graph-subscriptions:renew",
    "graph-subscriptions": "graph-subscriptions:renew",
}


def _has_issue(view: dict, prefix: str) -> bool:
    issues = view.get("issues")
    if issues is None:
        try:
            issues = feed_health.snapshot().get("issues") or {}
        except Exception:  # noqa: BLE001
            return False
    keys = issues.keys() if isinstance(issues, dict) else [
        (i or {}).get("key", "") for i in issues]
    return any(str(k).startswith(prefix) for k in keys)


def _check_feeds(view: dict, now: datetime | None = None) -> None:
    now = now or _utcnow()
    stale_keep, fail_keep = set(), set()
    for name, f in view["feeds"].items():
        label = FRIENDLY.get(name, "Feed '%s'" % name)
        if name == "watchdog":
            # It is running right now. If it STOPS, nothing here can say so:
            # the page banner (feeds view) and the off-box task do.
            continue
        if name in _OWN_ISSUE_FEEDS and _has_issue(view, _OWN_ISSUE_FEEDS[name]):
            continue   # the job already raised its own, more specific issue
        if f["stale"]:
            key = "feed-stale:" + name
            stale_keep.add(key)
            limit = f.get("max_age_hours")
            feed_health.raise_issue(
                key,
                "%s has not %s (expected at least every %s hours)."
                % (label,
                   ("completed since " + _when(f["last_ok"])) if f["last_ok"]
                   else "completed successfully yet",
                   ("%g" % limit) if limit else "few"),
                "error" if name in REQUIRED_FEEDS or name == "scheduler" else "warn")
        elif not f["ok"]:
            last_ok = _parse(f["last_ok"])
            if last_ok is not None and (
                    now - last_ok) < timedelta(hours=FAILING_GRACE_HOURS):
                continue
            key = "feed-failing:" + name
            fail_keep.add(key)
            feed_health.raise_issue(
                key, "%s ran %s but did not succeed%s."
                % (label, _when(f["last_attempt"]),
                   (": " + f["detail"][:160]) if f["detail"] else ""), "warn")
    feed_health.clear_issues_with_prefix("feed-stale:", stale_keep)
    feed_health.clear_issues_with_prefix("feed-failing:", fail_keep)


def _check_unparsed(now: datetime) -> None:
    keep = set()
    for e in it.load_unparsed_reports() or []:
        recv = _parse(e.get("received") or "")
        if recv is None or now - recv < timedelta(hours=UNPARSED_MAX_AGE_HOURS):
            continue
        key = "unparsed:" + _hash(e.get("id") or (e.get("sender", "")
                                                   + e.get("subject", "")))
        keep.add(key)
        feed_health.raise_issue(
            key, "A report email could not be read: \"%s\" from %s, received "
            "%s. Its count is not in the app."
            % (str(e.get("subject") or "(no subject)")[:80],
               e.get("sender") or "unknown sender", _when(e.get("received"))),
            "warn")
    feed_health.clear_issues_with_prefix("unparsed:", keep)


def _check_scan_po_failures() -> None:
    keep = set()
    rec = it.load_scan_health() or {}
    for err in (rec.get("po_failures") or [])[:15]:
        key = "scan-po-failure:" + _hash(err)
        keep.add(key)
        feed_health.raise_issue(
            key, "A purchase order email could not be read by the last scan: "
            "%s" % str(err)[:220], "error")
    feed_health.clear_issues_with_prefix("scan-po-failure:", keep)


def _check_warehouses(now: datetime, inv: dict) -> None:
    keep = set()
    visible = {k: v for k, v in inv.items() if not v.get("hidden")}
    rows = it.warehouse_freshness(now=datetime.now(), stale_days=WAREHOUSE_STALE_DAYS,
                                  inv=visible)
    for r in rows:
        # Only a warehouse that HAS reported and then stopped; one that has
        # never sent a count is a set-up question, not an alert.
        if not r.get("last_count_at") or not r.get("stale"):
            continue
        name = r.get("warehouse") or "Unassigned"
        key = "warehouse-stale:" + name
        keep.add(key)
        feed_health.raise_issue(
            key, "%s (%s) has not sent a count in %s days (last: %s)."
            % (name, r.get("distributor") or "?",
               int(r.get("days_since_count") or 0),
               str(r.get("last_count_at"))[:10]), "warn")
    feed_health.clear_issues_with_prefix("warehouse-stale:", keep)


def _check_disk() -> None:
    try:
        du = shutil.disk_usage(str(it.DATA_DIR))
    except OSError:
        return
    frac = du.free / du.total if du.total else 1.0
    if frac < DISK_MIN_FREE_FRACTION:
        feed_health.raise_issue(
            "disk-low", "The data disk is %d%% full (%d MB free)."
            % (round((1 - frac) * 100), du.free // 1048576), "error")
    else:
        feed_health.clear_issue("disk-low")


def books_check(inv: dict | None = None, usage: list | None = None) -> list:
    """Per SKU: last count + receipts since - burns/moves since == on-hand.

    Uses the SAME two helpers the count ingest uses to set on-hand
    (sync_inventory._receipts_after_count / _movement_after_count), so this
    is the app's own formula re-run, not a second opinion. Returns the
    mismatches, largest first: [{key, name, expected, quantity, gap}]."""
    from sync_inventory import _movement_after_count, _receipts_after_count
    inv = it.load_inventory() if inv is None else inv
    usage = it.load_usage() if usage is None else usage
    by_key: dict = {}
    for row in usage:
        by_key.setdefault(row.get("item_key") or "", []).append(row)
    out = []
    for key, item in inv.items():
        count_at = item.get("last_count_at") or ""
        counted = item.get("last_count_qty")
        if not count_at or counted is None:
            continue
        rows = by_key.get(key, [])
        try:
            expected = (float(counted)
                        + _receipts_after_count(rows, key, count_at)
                        - _movement_after_count(rows, key, count_at))
            qty = float(item.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        gap = qty - expected
        if abs(gap) > BOOKS_TOLERANCE_CS:
            out.append({"key": key, "name": item.get("name") or key,
                        "warehouse": item.get("warehouse") or "",
                        "expected": round(expected, 2),
                        "quantity": round(qty, 2), "gap": round(gap, 2)})
    out.sort(key=lambda r: abs(r["gap"]), reverse=True)
    return out


def _check_books(now: datetime, inv: dict, usage: list) -> int:
    rows = books_check(inv, usage)
    keep = set()
    for r in rows[:BOOKS_MAX_ISSUES]:
        key = "books:" + r["key"]
        keep.add(key)
        feed_health.raise_issue(
            key, "%s shows %g cs on hand, but its last count plus deliveries "
            "minus usage since comes to %g cs (%+g)."
            % (r["name"], r["quantity"], r["expected"], r["gap"]), "warn")
    feed_health.clear_issues_with_prefix("books:", keep)
    # Yesterday's burn rows exist at all (the burn job ran for that day).
    yday = (now - timedelta(days=1)).date().isoformat()
    burning = any(float(v.get("weekly_usage") or 0) > 0
                  and float(v.get("quantity") or 0) > 0 for v in inv.values())
    have = any(e.get("source") == "forecast-daily" and not e.get("reversed")
               and (e.get("forecast_date") or e.get("timestamp") or "")[:10] == yday
               for e in usage)
    if burning and not have:
        feed_health.raise_issue(
            "burn-missing", "No daily usage was taken off stock for %s." % yday,
            "warn")
    else:
        feed_health.clear_issue("burn-missing")
    return len(rows)


def _run_hooks(summary: dict) -> None:
    for mod_name in ("integrations.ingest_health", "integrations.po_health"):
        try:
            mod = __import__(mod_name, fromlist=["run_checks"])
        except ImportError:
            summary["hooks"][mod_name] = "not installed"
            continue
        try:
            mod.run_checks()
            summary["hooks"][mod_name] = "ok"
        except Exception as exc:  # noqa: BLE001
            summary["hooks"][mod_name] = "failed: %s" % type(exc).__name__
            print("[watchdog] %s.run_checks failed: %s: %s"
                  % (mod_name, type(exc).__name__, exc), file=sys.stderr)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def alerts_enabled() -> bool:
    return (os.environ.get("ALERTS_ENABLED", "on").strip().lower()
            not in {"off", "0", "false", "no"})


def _recipients() -> list:
    raw = os.environ.get("ALERT_EMAIL_TO") or "jd@hhbagels.com"
    return [a.strip() for a in raw.split(",") if a.strip()]


def _send_email(subject: str, body: str) -> tuple:
    """(sent, detail). Goes through the SAME view function as
    POST /api/email/send (blueprints.email.api_email_send), so its recipient
    allowlist and pinned sender apply here too; called in a nested request
    context rather than over HTTP, so it needs no token."""
    try:
        from flask import current_app
        try:
            flask_app = current_app._get_current_object()
        except RuntimeError:
            import app as _app_mod
            flask_app = _app_mod.app
        from blueprints.email import api_email_send
        payload = {"to": _recipients(), "subject": subject, "body": body}
        with flask_app.test_request_context("/api/email/send", method="POST",
                                            json=payload):
            resp = flask_app.make_response(api_email_send())
            data = resp.get_json(silent=True) or {}
        if data.get("ok") and data.get("sent"):
            return True, "sent to %s" % ", ".join(payload["to"])
        return False, str(data.get("error") or "send failed")[:300]
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def _state_path():
    return it.DATA_DIR / "watchdog_state.json"


def _load_state() -> dict:
    try:
        st = it._read_json(_state_path(), {})
    except Exception:  # noqa: BLE001
        st = {}
    if not isinstance(st, dict):
        st = {}
    st.setdefault("notified", {})
    return st


def _compose(lines: list, intro: str) -> str:
    out = [intro, ""]
    out += ["- " + ln for ln in lines]
    out += ["", "Open the app: " + _app_url(),
            "Details: %s/api/feeds/health" % _app_url(), "",
            "You get this once per problem; a summary follows each morning "
            "while anything is still open."]
    return "\n".join(out)


def _notify(now: datetime, digest: bool, send: bool, summary: dict) -> bool:
    """Email new issues (and the digest). Returns False when a send failed."""
    state = _load_state()
    cutoff = now - timedelta(hours=RENOTIFY_HOURS)
    state["notified"] = {k: v for k, v in state["notified"].items()
                         if (_parse(v) or now) >= cutoff - timedelta(days=7)}
    snap = feed_health.snapshot()
    issues = snap.get("issues") or {}
    new = {k: v for k, v in issues.items() if not v.get("notified_at")}
    # A problem that clears and comes back is "new" again to feed_health;
    # this keeps it to one email per key per 24 h.
    fresh = {k: v for k, v in new.items()
             if (_parse(state["notified"].get(k) or "") or
                 datetime.min.replace(tzinfo=timezone.utc)) < cutoff}
    today = now.date().isoformat()
    want_digest = bool(digest and issues and state.get("last_digest") != today)
    summary["new_issues"] = len(fresh)
    summary["open_issues"] = len(issues)
    if not (fresh or want_digest):
        if new and (send and alerts_enabled()):
            feed_health.mark_notified(list(new))     # repeats inside 24 h
        return True
    if not send or not alerts_enabled():
        summary["email"] = "not sent (alerts off)" if send else "not sent (send=false)"
        return True
    if want_digest:
        listed = issues
        intro = ("Daily summary: %d thing(s) in the Inventory Tracker still "
                 "need a look." % len(listed))
    else:
        listed = fresh
        intro = ("%d new thing(s) in the Inventory Tracker need a look."
                 % len(listed))
    order = {"error": 0, "warn": 1}
    lines = [v.get("message") or k for k, v in sorted(
        listed.items(), key=lambda kv: (order.get(kv[1].get("severity"), 2),
                                        kv[1].get("first_seen") or ""))]
    subject = "Inventory Tracker: %d thing(s) need a look" % len(listed)
    ok, detail = _send_email(subject, _compose(lines, intro))
    summary["email"] = detail
    if not ok:
        return False
    feed_health.mark_notified(list(new))
    stamp = _iso(now)
    for k in listed:
        state["notified"][k] = stamp
    if want_digest:
        state["last_digest"] = today
    try:
        it._write_json(_state_path(), state)
    except Exception as exc:  # noqa: BLE001
        print("[watchdog] could not save state: %s" % type(exc).__name__,
              file=sys.stderr)
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(now: datetime | None = None, *, books: bool | None = None,
        digest: bool | None = None, send: bool = True) -> dict:
    """One watchdog pass. `books` / `digest` default to the hour of day
    (07:xx / 13:xx UTC). Never raises."""
    now = now or _utcnow()
    books = (now.hour == BOOKS_HOUR_UTC) if books is None else bool(books)
    digest = (now.hour == DIGEST_HOUR_UTC) if digest is None else bool(digest)
    summary = {"ok": True, "at": _iso(now), "hooks": {}, "checks": {},
               "books_checked": books, "digest": digest}
    problems = []

    def _step(name, fn):
        try:
            res = fn()
            summary["checks"][name] = "ok" if res is None else res
        except Exception as exc:  # noqa: BLE001
            summary["checks"][name] = "failed: %s" % type(exc).__name__
            problems.append(name)
            print("[watchdog] %s failed: %s: %s"
                  % (name, type(exc).__name__, exc), file=sys.stderr)

    inv, usage = {}, []
    try:
        inv = it.load_inventory()
        feed_health.clear_issue("inventory-unreadable")
    except Exception as exc:  # noqa: BLE001
        problems.append("inventory")
        feed_health.raise_issue(
            "inventory-unreadable",
            "The inventory file could not be read (%s). Restore from a backup."
            % type(exc).__name__, "error")
    if inv == {} and "inventory" not in problems and it.INVENTORY_FILE.exists():
        feed_health.raise_issue(
            "inventory-empty", "The inventory is empty: 0 SKUs on file.", "error")
    else:
        feed_health.clear_issue("inventory-empty")

    _step("unparsed", lambda: _check_unparsed(now))
    _step("scan_po_failures", _check_scan_po_failures)
    _step("warehouses", lambda: _check_warehouses(now, inv))
    _step("disk", _check_disk)
    _run_hooks(summary)
    if books:
        def _books():
            return "%d mismatch(es)" % _check_books(now, inv, it.load_usage())
        _step("books", _books)
    # Feeds last, with this run counted: the watchdog is alive if it got here.
    feed_health.record("watchdog", True, "checks running")
    _step("feeds", lambda: _check_feeds(feeds_view(now), now))

    sent_ok = True
    try:
        sent_ok = _notify(now, digest, send, summary)
    except Exception as exc:  # noqa: BLE001
        sent_ok = False
        summary["email"] = "failed: %s" % type(exc).__name__
        print("[watchdog] notify failed: %s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
    summary["ok"] = sent_ok and not problems
    detail = ("alert email could not be sent: %s" % summary.get("email")
              if not sent_ok else
              "check(s) failed: %s" % ", ".join(problems) if problems else
              "%d open issue(s)" % summary.get("open_issues", 0))
    feed_health.record("watchdog", summary["ok"], detail)
    view = feeds_view(now)
    summary["stale_feeds"] = sorted(n for n, f in view["feeds"].items() if f["stale"])
    summary["issues"] = [i["key"] for i in view["issues"]]
    return summary
