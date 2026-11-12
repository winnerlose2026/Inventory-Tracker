"""Health blueprint — open, unauthenticated liveness probe.

First slice of the blueprint refactor. Deliberately has ZERO coupling to
app-level helpers, so it is the safe proof-of-pattern: a Flask Blueprint
defined here and registered on the app in app.py. The route's endpoint name
becomes ``health.healthz`` (blueprint-qualified), which is why app.py's
_OPEN_ENDPOINTS allowlist references ``health.healthz``.
"""
import os
import time
from pathlib import Path

from flask import Blueprint, jsonify

health_bp = Blueprint("health", __name__)


def _read_version() -> str:
    """Release version from the repo-root VERSION file ("unknown" if absent)."""
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


VERSION = _read_version()
# Render sets RENDER_GIT_COMMIT on every build; lets /healthz say exactly
# which commit is live so a release tag can be checked against production.
COMMIT = (os.environ.get("RENDER_GIT_COMMIT") or "")[:7] or None


@health_bp.route("/healthz")
def healthz():
    """Liveness probe for uptime monitors / load balancers. Touches no data."""
    return jsonify({
        "ok": True,
        "service": "inventory-tracker",
        "version": VERSION,
        "commit": COMMIT,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


# ---------------------------------------------------------------------------
# Readiness (open, reason codes only) and the authenticated deep check
# ---------------------------------------------------------------------------
SCHEDULER_HEARTBEAT_MAX_AGE_S = 15 * 60
READY_MIN_FREE_BYTES = 50 * 1024 * 1024


def _scheduler_enabled() -> bool:
    return (os.environ.get("INPROCESS_SCHEDULER", "on").strip().lower()
            not in {"off", "0", "false", "no"})


def _lock_probe(tries: int = 6, wait_s: float = 0.5) -> bool:
    """Can the data lock be taken? Non-blocking, retried for a few seconds so
    an ordinary save in progress is not reported as a stuck lock."""
    import inventory_tracker as it
    for i in range(tries):
        with it.data_lock(blocking=False) as held:
            if held:
                return True
        if i < tries - 1:
            time.sleep(wait_s)
    return False


def _readiness() -> dict:
    """Run the readiness checks. {"reasons": [codes], "details": {...}} --
    `details` is only ever returned by the authenticated deep check."""
    import shutil
    import tempfile

    import inventory_tracker as it
    reasons, details = [], {}

    # 1. data directory writable
    try:
        it.DATA_DIR.mkdir(exist_ok=True)
        fd, name = tempfile.mkstemp(dir=str(it.DATA_DIR), prefix=".readyz.")
        os.close(fd)
        os.unlink(name)
        details["data_dir_writable"] = True
    except OSError as exc:
        reasons.append("data_dir_not_writable")
        details["data_dir_writable"] = False
        details["data_dir_error"] = type(exc).__name__

    # 2. inventory parses and holds at least one SKU (or is not there yet)
    try:
        if it.INVENTORY_FILE.exists():
            inv = it._read_json(it.INVENTORY_FILE, {})
            details["sku_count"] = len(inv)
            if not inv:
                reasons.append("inventory_empty")
        else:
            details["sku_count"] = None
    except Exception as exc:  # noqa: BLE001
        reasons.append("inventory_unreadable")
        details["inventory_error"] = type(exc).__name__

    # 3. scheduler heartbeat
    if _scheduler_enabled():
        age = None
        try:
            from core import feed_health
            last = (feed_health.snapshot()["feeds"].get("scheduler") or {}).get("last_ok")
            if last:
                import calendar
                age = time.time() - calendar.timegm(
                    time.strptime(last, "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:  # noqa: BLE001
            age = None
        details["scheduler_heartbeat_age_s"] = None if age is None else round(age)
        if age is None:
            reasons.append("scheduler_no_heartbeat")
        elif age > SCHEDULER_HEARTBEAT_MAX_AGE_S:
            reasons.append("scheduler_heartbeat_stale")
    else:
        details["scheduler_heartbeat_age_s"] = "disabled"

    # 4. data lock
    try:
        free_lock = _lock_probe()
    except Exception:  # noqa: BLE001
        free_lock = False
    details["data_lock_free"] = free_lock
    if not free_lock:
        reasons.append("data_lock_busy")

    # 5. disk
    try:
        du = shutil.disk_usage(str(it.DATA_DIR))
        details["disk_total_bytes"] = du.total
        details["disk_free_bytes"] = du.free
        if du.free < READY_MIN_FREE_BYTES:
            reasons.append("disk_full")
    except OSError:
        reasons.append("disk_unreadable")
    return {"reasons": reasons, "details": details}


@health_bp.route("/readyz")
def readyz():
    """Is the app able to do its job? Open like /healthz, so it says only
    ok + reason codes -- never paths, counts or error text. 503 when not
    ready. The release workflow calls it after a deploy."""
    try:
        reasons = _readiness()["reasons"]
    except Exception:  # noqa: BLE001
        reasons = ["check_failed"]
    return jsonify({"ok": not reasons, "reasons": reasons, "commit": COMMIT,
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                   ), (200 if not reasons else 503)


@health_bp.route("/healthz/deep")
def healthz_deep():
    """Everything /readyz checks, with the details, plus data-file sizes,
    feed health and backups. Requires a login or token (the global gate)."""
    import inventory_tracker as it
    out = {"service": "inventory-tracker", "version": VERSION, "commit": COMMIT,
           "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        res = _readiness()
        out["ok"] = not res["reasons"]
        out["reasons"] = res["reasons"]
        out.update(res["details"])
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["reasons"] = ["check_failed"]
        out["error"] = type(exc).__name__
    files = {}
    try:
        for p in sorted(it.DATA_DIR.iterdir()):
            if p.is_file() and not p.name.startswith("."):
                st = p.stat()
                files[p.name] = {
                    "bytes": st.st_size,
                    "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(st.st_mtime))}
    except OSError:
        pass
    out["files"] = files
    try:
        out["usage_rows"] = len(it._read_json(it.USAGE_FILE, []))
    except Exception:  # noqa: BLE001
        out["usage_rows"] = None
    try:
        from integrations import watchdog
        view = watchdog.feeds_view()
        out["feeds_ok"] = view["ok"]
        out["stale_feeds"] = sorted(n for n, f in view["feeds"].items() if f["stale"])
        out["failing_feeds"] = sorted(n for n, f in view["feeds"].items()
                                      if not f["ok"] and not f["stale"])
        out["open_issues"] = len(view["issues"])
    except Exception:  # noqa: BLE001
        out["feeds_ok"] = None
    try:
        from integrations import backups
        rows = backups.list_snapshots()
        out["backups"] = {"count": len(rows),
                          "latest": rows[0] if rows else None,
                          "total_bytes": sum(r["size"] for r in rows)}
    except Exception:  # noqa: BLE001
        out["backups"] = None
    try:
        from flask import current_app
        import sys
        out["python"] = sys.version.split()[0]
        out["workers"] = os.environ.get("WEB_CONCURRENCY", "2")
        out["max_content_length"] = current_app.config.get("MAX_CONTENT_LENGTH")
    except Exception:  # noqa: BLE001
        pass
    return jsonify(out), (200 if out.get("ok") else 503)
