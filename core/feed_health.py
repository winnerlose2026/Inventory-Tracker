"""Shared health record for every feed and background job.

One small JSON file (data/feed_health.json) that every job writes to and the
watchdog, /api/scan/health and the UI banner read from. Two things live here:

* feeds:  {name: {last_attempt, last_ok, ok, detail, ...extra}} -- a job calls
  record(name, ok, detail) every time it runs, success or failure, so "it
  stopped running" and "it runs but fails" are both visible.
* issues: {key: {message, severity, first_seen, last_seen, notified_at}} --
  a specific problem a person must look at (a PO that would not parse, a held
  count, a stuck PO). raise_issue() is idempotent per key, so a problem that
  is seen on every 6-hour scan alerts ONCE (see new_issues / mark_notified),
  and clear_issue() removes it when it is resolved.

Never raises: a health write must not break the job it is reporting on.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import inventory_tracker as _it

FEED_HEALTH_FILE = _it.DATA_DIR / "feed_health.json"
_MAX_ISSUES = 300


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    try:
        rec = _it._read_json(_it.DATA_DIR / "feed_health.json", {})
    except Exception as exc:  # noqa: BLE001
        print(f"[feed_health] unreadable: {type(exc).__name__}", file=sys.stderr)
        rec = {}
    if not isinstance(rec, dict):
        rec = {}
    rec.setdefault("feeds", {})
    rec.setdefault("issues", {})
    return rec


def _save(rec: dict) -> None:
    _it._write_json(_it.DATA_DIR / "feed_health.json", rec)


def record(feed: str, ok: bool, detail: str = "", **extra) -> None:
    """Note that `feed` just ran. Call on success AND failure."""
    try:
        with _it.data_lock():
            rec = _load()
            cur = dict(rec["feeds"].get(feed) or {})
            now = _now()
            cur["last_attempt"] = now
            cur["ok"] = bool(ok)
            cur["detail"] = str(detail or "")[:500]
            if ok:
                cur["last_ok"] = now
            for k, v in extra.items():
                cur[k] = v
            rec["feeds"][feed] = cur
            _save(rec)
    except Exception as exc:  # noqa: BLE001
        print(f"[feed_health] record({feed}) failed: {type(exc).__name__}",
              file=sys.stderr)


def raise_issue(key: str, message: str, severity: str = "warn") -> bool:
    """Open (or refresh) an issue. Returns True when it is NEW."""
    try:
        with _it.data_lock():
            rec = _load()
            now = _now()
            cur = rec["issues"].get(key)
            is_new = cur is None
            if is_new:
                cur = {"first_seen": now, "notified_at": ""}
            cur["message"] = str(message or "")[:600]
            cur["severity"] = severity
            cur["last_seen"] = now
            rec["issues"][key] = cur
            if len(rec["issues"]) > _MAX_ISSUES:
                oldest = sorted(rec["issues"].items(),
                                key=lambda kv: kv[1].get("last_seen", ""))
                for k, _ in oldest[: len(rec["issues"]) - _MAX_ISSUES]:
                    rec["issues"].pop(k, None)
            _save(rec)
            return is_new
    except Exception as exc:  # noqa: BLE001
        print(f"[feed_health] raise_issue({key}) failed: {type(exc).__name__}",
              file=sys.stderr)
        return False


def clear_issue(key: str) -> None:
    try:
        with _it.data_lock():
            rec = _load()
            if rec["issues"].pop(key, None) is not None:
                _save(rec)
    except Exception as exc:  # noqa: BLE001
        print(f"[feed_health] clear_issue({key}) failed: {type(exc).__name__}",
              file=sys.stderr)


def clear_issues_with_prefix(prefix: str, keep: set | None = None) -> None:
    """Drop every issue whose key starts with `prefix` except those in `keep`
    -- for a job that re-derives its whole problem list each run."""
    keep = keep or set()
    try:
        with _it.data_lock():
            rec = _load()
            gone = [k for k in rec["issues"] if k.startswith(prefix) and k not in keep]
            for k in gone:
                rec["issues"].pop(k, None)
            if gone:
                _save(rec)
    except Exception as exc:  # noqa: BLE001
        print(f"[feed_health] clear prefix {prefix} failed: {type(exc).__name__}",
              file=sys.stderr)


def snapshot() -> dict:
    """{"feeds": {...}, "issues": {...}} -- a copy, safe to mutate."""
    return _load()


def new_issues() -> dict:
    """Issues nobody has been told about yet."""
    return {k: v for k, v in _load()["issues"].items() if not v.get("notified_at")}


def mark_notified(keys) -> None:
    try:
        with _it.data_lock():
            rec = _load()
            now = _now()
            for k in keys:
                if k in rec["issues"]:
                    rec["issues"][k]["notified_at"] = now
            _save(rec)
    except Exception as exc:  # noqa: BLE001
        print(f"[feed_health] mark_notified failed: {type(exc).__name__}",
              file=sys.stderr)
