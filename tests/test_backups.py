"""Snapshots of data/: create, list, retention, download, restore."""
import io
import json
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

import inventory_tracker as it
from core import feed_health
from integrations import backups

TOKEN = "tok"
H = {"X-Inventory-Token": TOKEN}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKEN", TOKEN)
    monkeypatch.delenv("INVENTORY_API_TOKENS", raising=False)
    monkeypatch.delenv("INVENTORY_ADMINS", raising=False)
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _seed(qty=5, rows=3):
    it._save(it.INVENTORY_FILE, {"a": {"name": "A", "quantity": qty},
                                 "b": {"name": "B", "quantity": 1}})
    it._save(it.USAGE_FILE, [{"item_key": "a", "amount": 1}] * rows)
    it.save_production([{"po": "1"}])
    it.save_canceled_pos({"X": {}})


def test_snapshot_holds_every_json_file_and_nothing_else():
    _seed()
    (it.DATA_DIR / "junk.tmp.1").write_text("x")
    res = backups.make_snapshot("hourly")
    assert res["ok"] and backups.NAME_RE.match(res["name"])
    members = backups.read_snapshot(res["name"])
    assert {"inventory.json", "usage.json", "production.json",
            "canceled_pos.json", "MANIFEST.json"} <= set(members)
    assert not any(n.endswith((".bak", ".lock")) or ".tmp." in n for n in members)
    man = json.loads(members["MANIFEST.json"])
    assert man["files"]["inventory.json"]["skus"] == 2
    assert man["files"]["usage.json"]["rows"] == 3
    assert feed_health.snapshot()["feeds"]["backup"]["ok"] is True
    assert not [p for p in backups.backup_dir().iterdir() if p.name.startswith(".")]


def test_snapshot_is_skipped_and_flagged_when_disk_is_nearly_full(monkeypatch):
    _seed()
    monkeypatch.setattr(backups, "free_bytes", lambda: 100 * 1024 * 1024)
    res = backups.make_snapshot("hourly")
    assert not res["ok"] and res["skipped"] == "disk"
    assert backups.list_snapshots() == []
    snap = feed_health.snapshot()
    assert "backup-disk-low" in snap["issues"]
    assert snap["feeds"]["backup"]["ok"] is False


def test_retention_keeps_24_hourly_14_daily_8_weekly_and_pre_for_30_days():
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
    rows = []
    for h in range(24 * 120):                       # hourly for 120 days
        ts = now - timedelta(hours=h)
        rows.append({"name": ts.strftime("%Y%m%dT%H2000Z") + "-hourly.tar.gz"})
    rows.append({"name": (now - timedelta(days=29)).strftime("%Y%m%dT%H%M%SZ")
                 + "-pre-restore.tar.gz"})
    rows.append({"name": (now - timedelta(days=31)).strftime("%Y%m%dT%H%M%SZ")
                 + "-pre-restore.tar.gz"})
    keep, drop = backups.plan_retention(rows, now)
    pre = sorted(n for n in keep if "pre-restore" in n)
    assert len(pre) == 1
    regular = sorted((n for n in keep if "hourly" in n), reverse=True)
    # 24 hourly; daily adds 13 older days; weekly adds the older weeks
    assert 24 + 13 <= len(regular) <= 24 + 13 + 8
    assert regular[:24] == sorted((r["name"] for r in rows if "hourly" in r["name"]),
                                  reverse=True)[:24]
    oldest = backups._parse_name(regular[-1])[0]
    assert timedelta(days=42) <= now - oldest <= timedelta(days=63)
    assert len(keep) + len(drop) == len(rows)


def test_prune_deletes_only_what_retention_drops():
    _seed()
    bdir = backups.backup_dir(); bdir.mkdir(parents=True)
    old = bdir / "20200101T000000Z-pre-restore.tar.gz"
    old.write_bytes(b"x")
    keepme = bdir / "notes.txt"; keepme.write_text("mine")
    res = backups.make_snapshot("hourly")
    assert old.name in res["pruned"] and not old.exists()
    assert keepme.exists() and backups.resolve(res["name"])


def test_resolve_rejects_anything_not_in_the_listing():
    _seed()
    name = backups.make_snapshot("manual")["name"]
    assert backups.resolve(name)
    for bad in ("../inventory.json", "..%2Finventory.json", name + "/../x",
                "20200101T000000Z-manual.tar.gz", "", None, name.upper()):
        assert backups.resolve(bad) is None


def test_endpoints_list_download_and_auth(client, monkeypatch):
    _seed()
    assert client.post("/api/admin/backup/run", json={}).status_code == 401
    r = client.post("/api/admin/backup/run", json={"label": "Before Fix!"}, headers=H)
    name = r.get_json()["name"]
    assert r.status_code == 200 and name.endswith("-before-fix.tar.gz")
    lst = client.get("/api/admin/backups", headers=H).get_json()
    assert lst["count"] == 1 and lst["backups"][0]["name"] == name
    assert set(lst["backups"][0]) >= {"name", "size", "ts"}
    for url in ("/api/admin/backups/latest", "/api/admin/backups/" + name):
        d = client.get(url, headers=H)
        assert d.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(d.data), mode="r:gz") as tar:
            assert "inventory.json" in tar.getnames()
    # a slash never reaches the view (no route); a dotted name is not listed
    assert client.get("/api/admin/backups/..%2F..%2Finventory.json",
                      headers=H).status_code in (404, 405)
    assert client.get("/api/admin/backups/..", headers=H).status_code in (404, 405)
    assert client.get("/api/admin/backups/inventory.json", headers=H).status_code == 404
    assert client.get("/api/admin/backups/nope.tar.gz", headers=H).status_code == 404
    # a `backup` key can run / list / download / ack, but never restore
    monkeypatch.setenv("INVENTORY_API_TOKENS", "offsite:bbb:backup,viewer:rrr:read")
    B = {"X-Inventory-Token": "bbb"}
    assert client.get("/api/admin/backups/latest", headers=B).status_code == 200
    assert client.post(f"/api/admin/backups/{name}/restore", json={},
                       headers=B).status_code == 403
    assert client.get("/api/admin/backups", headers={"X-Inventory-Token": "rrr"}
                      ).status_code == 403
    r = client.post("/api/admin/backup/offsite-ack", json={"name": name}, headers=B)
    assert r.get_json() == {"ok": True, "name": name, "known": True}
    assert feed_health.snapshot()["feeds"]["backup-offsite"]["last_name"] == name
    assert client.post("/api/admin/backup/offsite-ack", json={"name": "../x"},
                       headers=B).status_code == 400


def test_non_admin_login_cannot_touch_backups(client, monkeypatch):
    _seed()
    monkeypatch.setenv("INVENTORY_ADMINS", "jd")
    with client.session_transaction() as s:
        s["user"] = "jay"
    assert client.get("/api/admin/backups").status_code == 403
    assert client.post("/api/admin/backup/run", json={}).status_code == 403
    with client.session_transaction() as s:
        s["user"] = "JD"
    assert client.get("/api/admin/backups").status_code == 200


def test_restore_is_a_dry_run_by_default_then_restores_in_one_commit(client):
    _seed(qty=5, rows=3)
    name = backups.make_snapshot("manual")["name"]
    it._save(it.INVENTORY_FILE, {"a": {"name": "A", "quantity": 99},
                                 "c": {"name": "C", "quantity": 2}})
    it._save(it.USAGE_FILE, [])
    it._save(it.DATA_DIR / "feed_health.json", {"feeds": {"x": {}}, "issues": {}})
    r = client.post(f"/api/admin/backups/{name}/restore", json={}, headers=H)
    d = r.get_json()
    assert r.status_code == 200 and d["dry_run"] is True
    inv = d["files"]["inventory.json"]
    assert (inv["skus_added"], inv["skus_dropped"], inv["skus_quantity_changed"]) == (1, 1, 1)
    assert inv["current"]["skus"] == 2 and inv["snapshot"]["skus"] == 2
    assert d["files"]["usage.json"]["rows_delta"] == 3
    assert d["files"]["production.json"]["action"] == "unchanged"
    assert it.load_usage() == []                               # nothing written
    r = client.post(f"/api/admin/backups/{name}/restore",
                    json={"dry_run": False}, headers=H).get_json()
    assert r["ok"] and r["written"] == ["inventory.json", "usage.json"]
    assert it._read_json(it.INVENTORY_FILE, {})["a"]["quantity"] == 5
    assert len(it.load_usage()) == 3
    assert not (it.DATA_DIR / ".txn").exists()
    # the state from just before the restore is itself recoverable
    pre = backups.read_snapshot(r["pre_restore_snapshot"])
    assert json.loads(pre["inventory.json"])["a"]["quantity"] == 99
    # live alert state was not wound back
    assert "x" in it._read_json(it.DATA_DIR / "feed_health.json", {})["feeds"]


def test_restore_refuses_when_the_pre_restore_snapshot_fails(client, monkeypatch):
    _seed()
    name = backups.make_snapshot("manual")["name"]
    it._save(it.USAGE_FILE, [])
    monkeypatch.setattr(backups, "free_bytes", lambda: 1)
    r = client.post(f"/api/admin/backups/{name}/restore",
                    json={"dry_run": False}, headers=H)
    assert r.status_code == 500 and "nothing was restored" in r.get_json()["error"]
    assert it.load_usage() == []


def test_upload_of_an_offsite_copy_can_be_restored(client):
    _seed(qty=7)
    name = backups.make_snapshot("hourly")["name"]
    raw = (backups.backup_dir() / name).read_bytes()
    (backups.backup_dir() / name).unlink()                    # disk was lost
    it._save(it.INVENTORY_FILE, {"z": {"name": "Z", "quantity": 0}})
    r = client.post("/api/admin/backups/upload?name=" + name, data=raw, headers=H)
    up = r.get_json()
    assert r.status_code == 200 and up["name"].endswith("-upload.tar.gz")
    client.post(f"/api/admin/backups/{up['name']}/restore",
                json={"dry_run": False}, headers=H)
    assert it._read_json(it.INVENTORY_FILE, {})["a"]["quantity"] == 7
    assert client.post("/api/admin/backups/upload", data=b"not a tarball",
                       headers=H).status_code == 400


def test_archive_members_with_paths_are_ignored():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for n in ("../evil.json", "sub/x.json", "ok.json"):
            data = b"{}"
            info = tarfile.TarInfo(n); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    assert set(backups._read_members(buf)) == {"ok.json"}
