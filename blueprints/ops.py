"""Operations blueprint -- backups, feed health and the watchdog.

Everything here is about keeping the data safe and saying out loud when
something has stopped: snapshots of data/ (integrations/backups.py), the
shared feed-health record (core/feed_health.py) the UI banner reads, and the
hourly watchdog (integrations/watchdog.py).
"""
from flask import Blueprint, jsonify, request, send_file

from core.auth import admin_required, is_admin, token_identity
from core.errors import _safe_err

ops_bp = Blueprint("ops", __name__)


def _backup_access():
    """Backups hold every data file: an admin login, a full-access token, or
    a named token with the `backup` scope (run / list / download only)."""
    if is_admin():
        return True
    ident = token_identity()
    return bool(ident) and "backup" in ident[1]


def _deny():
    return jsonify({"ok": False, "error": "admin only"}), 403


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

@ops_bp.route("/api/admin/backup/run", methods=["POST"])
def api_backup_run():
    """Take a snapshot now. Body: {label?} (default "manual")."""
    if not _backup_access():
        return _deny()
    from integrations import backups
    body = request.get_json(silent=True) or {}
    res = backups.make_snapshot(str(body.get("label") or "manual"))
    res.pop("files", None)
    return jsonify(res), (200 if res.get("ok") else
                          507 if res.get("skipped") == "disk" else 500)


@ops_bp.route("/api/admin/backups")
def api_backups_list():
    if not _backup_access():
        return _deny()
    from integrations import backups
    rows = backups.list_snapshots()
    free = backups.free_bytes()
    return jsonify({"ok": True, "count": len(rows), "backups": rows,
                    "total_bytes": sum(r["size"] for r in rows),
                    "disk_free_bytes": free})


def _send(name):
    from integrations import backups
    path = backups.resolve(name)
    if path is None:
        return jsonify({"ok": False, "error": "no such backup"}), 404
    return send_file(str(path), as_attachment=True, download_name=path.name,
                     mimetype="application/gzip", max_age=0)


@ops_bp.route("/api/admin/backups/latest")
def api_backups_latest():
    """Download the newest snapshot (what the off-box task pulls daily)."""
    if not _backup_access():
        return _deny()
    from integrations import backups
    row = backups.latest()
    if not row:
        return jsonify({"ok": False, "error": "no backups yet"}), 404
    resp = _send(row["name"])
    try:
        resp.headers["X-Backup-Name"] = row["name"]
    except AttributeError:
        pass
    return resp


@ops_bp.route("/api/admin/backups/<name>")
def api_backups_download(name):
    if not _backup_access():
        return _deny()
    return _send(name)


@ops_bp.route("/api/admin/backups/<name>/restore", methods=["POST"])
@admin_required
def api_backups_restore(name):
    """Restore a snapshot. Body: {dry_run=true, files?: [names]}.

    dry_run defaults to TRUE and returns a per-file summary of what would
    change. A live run ({"dry_run": false}) snapshots the current data first
    ("pre-restore") and writes every file as one commit."""
    from integrations import backups
    body = request.get_json(silent=True) or {}
    dry_run = body.get("dry_run", True) is not False
    files = body.get("files")
    if files is not None and not isinstance(files, list):
        return jsonify({"ok": False, "error": "files must be a list"}), 400
    try:
        res = backups.restore(name, dry_run=dry_run, files=files)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": _safe_err(exc)}), 500
    status = res.pop("status", 200 if res.get("ok") else 500)
    return jsonify(res), status


@ops_bp.route("/api/admin/backups/upload", methods=["POST"])
@admin_required
def api_backups_upload():
    """Hand back an archive that was downloaded earlier, so it can be
    restored after the disk itself was lost. Raw body = the .tar.gz
    (Content-Type: application/gzip); optional ?name=<original file name>.
    Stores it only -- restoring is a separate, explicit call."""
    from integrations import backups
    raw = request.get_data(cache=False)
    if not raw:
        return jsonify({"ok": False, "error": "empty body"}), 400
    try:
        res = backups.store_upload(raw, request.args.get("name") or "")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False,
                        "error": "not a usable backup archive: "
                                 + type(exc).__name__}), 400
    return jsonify(res)


@ops_bp.route("/api/admin/backup/offsite-ack", methods=["POST"])
def api_backup_offsite_ack():
    """The off-box task calls this after it has stored a downloaded snapshot
    somewhere else. Body: {name}. Records feed "backup-offsite"."""
    if not _backup_access():
        return _deny()
    from core import feed_health
    from integrations import backups
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "")
    if backups.NAME_RE.match(name) is None:
        return jsonify({"ok": False, "error": "name must be a backup name"}), 400
    known = backups.resolve(name) is not None
    feed_health.record("backup-offsite", True, name, last_name=name,
                       still_on_disk=known)
    feed_health.clear_issue("feed-stale:backup-offsite")
    return jsonify({"ok": True, "name": name, "known": known})
