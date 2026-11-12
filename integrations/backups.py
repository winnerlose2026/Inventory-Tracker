"""Point-in-time snapshots of every data file, with retention and restore.

Before this module the only protection was the one-deep <name>.bak beside
each file, and the only off-box copy was a laptop task saving three API
responses. usage.json (the only record of arrived POs), production.json,
po_delivery*.json, chefs_warehouse_pos.json, canceled_pos.json,
po_status_overrides.json and freight_invoices.json were backed up nowhere.

A snapshot is data/backups/<UTC ts>-<label>.tar.gz holding every data/*.json
(plus activity_log.jsonl) and a MANIFEST.json. It is written atomically
(.partial -> rename). The data lock is held only while the file bytes are
copied to a staging folder; compression happens outside it.

Retention, applied on every run: the newest snapshot of each of the last 24
hours, 14 days and 8 ISO weeks that have one, plus every "pre-*" / "upload*"
snapshot for 30 days.

Nothing here raises to a scheduler: make_snapshot() reports through its
return value, core.feed_health ("backup") and issues.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import inventory_tracker as it
from core import feed_health

MIN_FREE_BYTES = 150 * 1024 * 1024
KEEP_HOURLY = 24
KEEP_DAILY = 14
KEEP_WEEKLY = 8
KEEP_PRE_DAYS = 30
MAX_MEMBER_BYTES = 200 * 1024 * 1024

NAME_RE = re.compile(r"^(\d{8}T\d{6}Z)-([a-z0-9][a-z0-9-]{0,39})\.tar\.gz$")
_MEMBER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.(json|jsonl)$")
MANIFEST = "MANIFEST.json"

# Restored only when asked for by name: live state that must not be wound
# back with the stock data (open alerts, the Graph subscription ids Microsoft
# currently holds for us).
RESTORE_SKIP_BY_DEFAULT = ("feed_health.json", "graph_subscriptions.json")


def backup_dir() -> Path:
    return it.DATA_DIR / "backups"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_label(label: str) -> str:
    lab = re.sub(r"[^a-z0-9-]+", "-", str(label or "manual").lower()).strip("-")
    return (lab or "manual")[:40]


def _parse_name(name: str):
    m = NAME_RE.match(name or "")
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    return ts, m.group(2)


def _is_kept_long(label: str) -> bool:
    return label.startswith("pre-") or label.startswith("upload")


def _data_files() -> list:
    """Every live data file worth keeping: data/*.json and *.jsonl. The .bak,
    .tmp.*, .corrupt-* and lock files do not match; backups/ is a folder."""
    try:
        files = [p for p in it.DATA_DIR.iterdir()
                 if p.is_file() and _MEMBER_RE.match(p.name)]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.name)


def _summarize(name: str, raw: bytes) -> dict:
    """Small description of one data file for the manifest / restore diff."""
    out = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if not name.endswith(".json"):
        out["lines"] = raw.count(b"\n")
        return out
    try:
        doc = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        out["valid"] = False
        return out
    out["valid"] = True
    out.update(_describe(name, doc))
    return out


def _describe(name: str, doc) -> dict:
    d = {"type": type(doc).__name__}
    if isinstance(doc, (list, dict)):
        d["count"] = len(doc)
    if name == "inventory.json" and isinstance(doc, dict):
        d["skus"] = len(doc)
        d["total_quantity"] = round(sum(
            float((v or {}).get("quantity") or 0) for v in doc.values()
            if isinstance(v, dict)), 2)
        d["on_order_rows"] = sum(len((v or {}).get("on_order") or [])
                                 for v in doc.values() if isinstance(v, dict))
    elif name == "usage.json" and isinstance(doc, list):
        d["rows"] = len(doc)
    return d


def free_bytes() -> int:
    try:
        it.DATA_DIR.mkdir(exist_ok=True)
        return shutil.disk_usage(str(it.DATA_DIR)).free
    except OSError:
        return -1


def list_snapshots() -> list:
    """Newest first: [{name, size, ts, label}]."""
    out = []
    try:
        entries = list(backup_dir().iterdir())
    except OSError:
        return out
    for p in entries:
        parsed = _parse_name(p.name)
        if not parsed or not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        out.append({"name": p.name, "size": size,
                    "ts": parsed[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "label": parsed[1]})
    out.sort(key=lambda r: r["name"], reverse=True)
    return out


def resolve(name: str):
    """Path of a snapshot by its listed name, or None. The name must match
    the strict pattern AND be one the listing returned, so nothing a caller
    sends is ever joined onto a path unchecked."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None
    for row in list_snapshots():
        if row["name"] == name:
            return backup_dir() / row["name"]
    return None


def latest():
    rows = list_snapshots()
    return rows[0] if rows else None


def make_snapshot(label: str = "manual", *, record_feed: bool = True) -> dict:
    """Create a snapshot. Returns {"ok": True, "name", "size", "files", ...}
    or {"ok": False, "error": ..., "skipped": reason}. Never raises."""
    label = _clean_label(label)
    try:
        result = _make_snapshot(label)
    except Exception as exc:  # noqa: BLE001
        print("[backups] snapshot failed: %s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    if result.get("ok"):
        feed_health.clear_issue("backup-failed")
        feed_health.clear_issue("backup-disk-low")
        try:
            result["pruned"] = prune()
        except Exception as exc:  # noqa: BLE001
            print("[backups] prune failed: %s" % type(exc).__name__,
                  file=sys.stderr)
            result["pruned"] = []
    elif result.get("skipped") == "disk":
        feed_health.raise_issue(
            "backup-disk-low",
            "Backups are paused: the data disk has under 150 MB free "
            "(%s MB)." % result.get("free_mb"), "error")
    else:
        feed_health.raise_issue(
            "backup-failed",
            "The last backup did not complete (%s)."
            % str(result.get("error") or "unknown")[:200], "error")
    if record_feed:
        feed_health.record(
            "backup", bool(result.get("ok")),
            (result.get("name") or result.get("error") or "")[:300],
            **({"last_name": result["name"], "last_size": result["size"]}
               if result.get("ok") else {}))
    return result


def _make_snapshot(label: str) -> dict:
    free = free_bytes()
    if 0 <= free < MIN_FREE_BYTES:
        return {"ok": False, "skipped": "disk",
                "free_mb": round(free / 1048576, 1),
                "error": "less than 150 MB free on the data disk"}
    bdir = backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    now = _utcnow()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    name = "%s-%s.tar.gz" % (stamp, label)
    n = 1
    while (bdir / name).exists():        # two snapshots in one second
        n += 1
        name = "%s-%s.tar.gz" % (stamp, _clean_label("%s-%d" % (label[:36], n)))
    stage = bdir / (".staging-%s-%d" % (stamp, os.getpid()))
    partial = bdir / ("." + name + ".partial")
    try:
        stage.mkdir(parents=True, exist_ok=False)
        staged = []
        # The lock is held only for the byte copy, so the set of files is one
        # consistent moment (no half-finished inventory + usage pair) without
        # blocking the app for the whole compression.
        with it.data_lock():
            for src in _data_files():
                dst = stage / src.name
                try:
                    shutil.copyfile(src, dst)
                    staged.append(dst)
                except OSError as exc:
                    print("[backups] could not copy %s: %s" % (src.name, exc),
                          file=sys.stderr)
        if not staged:
            return {"ok": False, "skipped": "empty",
                    "error": "no data files to back up yet"}
        manifest = {"created": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "label": label,
                    "commit": (os.environ.get("RENDER_GIT_COMMIT") or "")[:7],
                    "files": {}}
        with open(partial, "wb") as raw:
            with tarfile.open(fileobj=raw, mode="w:gz") as tar:
                for p in staged:
                    data = p.read_bytes()
                    manifest["files"][p.name] = _summarize(p.name, data)
                    info = tarfile.TarInfo(p.name)
                    info.size = len(data)
                    info.mtime = int(p.stat().st_mtime)
                    tar.addfile(info, io.BytesIO(data))
                mbytes = json.dumps(manifest, indent=2).encode("utf-8")
                info = tarfile.TarInfo(MANIFEST)
                info.size = len(mbytes)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(mbytes))
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(partial, bdir / name)
        it._fsync_dir(bdir)
        size = (bdir / name).stat().st_size
        return {"ok": True, "name": name, "size": size, "label": label,
                "ts": manifest["created"], "files": manifest["files"]}
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        try:
            partial.unlink()
        except OSError:
            pass


def plan_retention(rows: list, now: datetime | None = None) -> tuple:
    """(keep, drop) name sets for a listing. Pure, so it can be tested."""
    now = now or _utcnow()
    keep, regular = set(), []
    for r in rows:
        parsed = _parse_name(r["name"])
        if not parsed:
            continue
        ts, label = parsed
        if _is_kept_long(label):
            if now - ts <= timedelta(days=KEEP_PRE_DAYS):
                keep.add(r["name"])
        else:
            regular.append((ts, r["name"]))
    regular.sort(reverse=True)                      # newest first
    for fmt, limit in (("%Y%m%d%H", KEEP_HOURLY), ("%Y%m%d", KEEP_DAILY),
                       ("%G%V", KEEP_WEEKLY)):
        seen = []
        for ts, name in regular:
            bucket = ts.strftime(fmt)
            if bucket in seen:
                continue
            if len(seen) >= limit:
                break
            seen.append(bucket)
            keep.add(name)                          # newest in its bucket
    names = {r["name"] for r in rows if _parse_name(r["name"])}
    return keep, names - keep


def prune(now: datetime | None = None) -> list:
    """Apply retention; also clear staging folders / partial files more than
    an hour old (a snapshot that was killed mid-way). Returns removed names."""
    removed = []
    bdir = backup_dir()
    _keep, drop = plan_retention(list_snapshots(), now)
    for name in sorted(drop):
        try:
            (bdir / name).unlink()
            removed.append(name)
        except OSError as exc:
            print("[backups] could not delete %s: %s" % (name, exc),
                  file=sys.stderr)
    # Plain per-file copies the admin endpoints leave beside the archives
    # (inventory-<ts>-pre-seed-reset.json ...): same 30 days as "pre-*".
    old_copy = time.time() - KEEP_PRE_DAYS * 86400
    try:
        for p in bdir.glob("*-pre-*.json"):
            try:
                if p.is_file() and p.stat().st_mtime < old_copy:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        pass
    cutoff = time.time() - 3600
    try:
        for p in bdir.iterdir():
            if not (p.name.startswith(".staging-") or p.name.endswith(".partial")):
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink()
            except OSError:
                continue
    except OSError:
        pass
    return removed


def _read_members(fileobj_or_path) -> dict:
    """{member name: bytes} for the safe, flat members of a snapshot archive.
    Never extracts to disk; anything with a path, a link or an odd name is
    ignored."""
    out = {}
    kw = ({"name": str(fileobj_or_path)} if isinstance(fileobj_or_path, (str, Path))
          else {"fileobj": fileobj_or_path})
    with tarfile.open(mode="r:gz", **kw) as tar:
        for m in tar:
            if not m.isfile() or m.size > MAX_MEMBER_BYTES:
                continue
            if m.name != MANIFEST and not _MEMBER_RE.match(m.name):
                continue
            fh = tar.extractfile(m)
            if fh is not None:
                out[m.name] = fh.read()
    return out


def read_snapshot(name: str) -> dict:
    path = resolve(name)
    if path is None:
        raise FileNotFoundError(name)
    return _read_members(path)


def store_upload(raw: bytes, original_name: str = "") -> dict:
    """Keep an archive that was downloaded earlier (the off-box copy) so it
    can be restored after the disk itself was lost. Validated first: it must
    be a snapshot this module could have written."""
    members = _read_members(io.BytesIO(raw))
    docs = [n for n in members if n.endswith(".json") and n != MANIFEST]
    if not docs:
        raise ValueError("archive holds no data files")
    for n in docs:
        json.loads(members[n].decode("utf-8"))      # raises on a bad file
    parsed = _parse_name(os.path.basename(original_name or ""))
    stamp = (parsed[0] if parsed else _utcnow()).strftime("%Y%m%dT%H%M%SZ")
    bdir = backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    name = "%s-upload.tar.gz" % stamp
    n = 1
    while (bdir / name).exists():
        n += 1
        name = "%s-upload-%d.tar.gz" % (stamp, n)
    partial = bdir / ("." + name + ".partial")
    with open(partial, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(partial, bdir / name)
    return {"ok": True, "name": name, "size": len(raw), "files": sorted(docs)}


def _inventory_diff(cur, new) -> dict:
    cur = cur if isinstance(cur, dict) else {}
    new = new if isinstance(new, dict) else {}
    added = sorted(set(new) - set(cur))
    dropped = sorted(set(cur) - set(new))
    changed = sorted(
        k for k in set(cur) & set(new)
        if abs(float((cur[k] or {}).get("quantity") or 0)
               - float((new[k] or {}).get("quantity") or 0)) > 1e-9)
    return {"skus_added": len(added), "skus_dropped": len(dropped),
            "skus_quantity_changed": len(changed),
            "added_keys": added[:20], "dropped_keys": dropped[:20],
            "changed_keys": changed[:20]}


def restore(name: str, *, dry_run: bool = True, files=None) -> dict:
    """Put a snapshot's data files back.

    dry_run (the default) only reports, per file, what is there now and what
    the snapshot holds. A live run first takes a "pre-restore" snapshot (no
    snapshot, no restore) and then writes every file in ONE journaled commit.
    Files that exist now but are not in the snapshot are left alone.
    """
    path = resolve(name)
    if path is None:
        return {"ok": False, "error": "no such backup", "status": 404}
    try:
        members = _read_members(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "backup is unreadable: %s"
                % type(exc).__name__, "status": 422}
    wanted = None
    if files:
        wanted = {str(f) for f in files}
    plan, docs, problems = {}, {}, []
    with it.data_lock():
        for fname in sorted(members):
            if fname == MANIFEST or not fname.endswith(".json"):
                continue
            if wanted is not None and fname not in wanted:
                continue
            if wanted is None and fname in RESTORE_SKIP_BY_DEFAULT:
                plan[fname] = {"action": "skipped (live state; name it in "
                                         "`files` to restore it)"}
                continue
            try:
                doc = json.loads(members[fname].decode("utf-8"))
            except Exception:  # noqa: BLE001
                problems.append("%s in the backup is not valid JSON" % fname)
                continue
            target = it.DATA_DIR / fname
            entry = {"snapshot": _describe(fname, doc)}
            try:
                cur = it._read_json(target, None)
                entry["current"] = (_describe(fname, cur) if cur is not None
                                    else {"missing": True})
            except Exception as exc:  # noqa: BLE001
                cur = None
                entry["current"] = {"unreadable": type(exc).__name__}
            if fname == "inventory.json":
                entry.update(_inventory_diff(cur, doc))
            if isinstance(cur, list) and isinstance(doc, list):
                entry["rows_delta"] = len(doc) - len(cur)
            entry["action"] = ("unchanged" if cur == doc else "replace")
            plan[fname] = entry
            if cur != doc:
                docs[target] = doc
        if wanted:
            for f in sorted(wanted - set(members)):
                problems.append("%s is not in this backup" % f)
        present = {p.name for p in _data_files()}
        not_in_snapshot = sorted(present - set(members))
        result = {"ok": not problems, "dry_run": bool(dry_run), "name": name,
                  "files": plan, "would_write": sorted(p.name for p in docs),
                  "not_in_snapshot": not_in_snapshot, "problems": problems}
        if problems:
            result["status"] = 422
            result["error"] = "; ".join(problems)
            return result
        if dry_run:
            return result
        pre = make_snapshot("pre-restore", record_feed=False)
        if not pre.get("ok") and pre.get("skipped") != "empty":
            return {"ok": False, "status": 500, "dry_run": False, "name": name,
                    "error": "could not snapshot the current data first (%s); "
                             "nothing was restored" % pre.get("error")}
        result["pre_restore_snapshot"] = pre.get("name")
        if docs:
            it.commit_state(docs)
        result["written"] = sorted(p.name for p in docs)
        return result
