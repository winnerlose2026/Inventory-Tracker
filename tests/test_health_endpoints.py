"""/healthz stays shallow; /readyz gives reason codes; /healthz/deep is gated."""
import collections
import time

import pytest

import inventory_tracker as it
from core import feed_health

TOKEN = "tok"
H = {"X-Inventory-Token": TOKEN}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKEN", TOKEN)
    monkeypatch.setenv("INPROCESS_SCHEDULER", "on")
    import app as app_mod
    import blueprints.health as health
    DU = collections.namedtuple("DU", "total used free")
    import shutil
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: DU(1000 * 2**20, 100 * 2**20, 900 * 2**20))
    monkeypatch.setattr(health.time, "sleep", lambda s: None)
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _ready():
    it.save_inventory({"a": {"name": "A", "quantity": 1}})
    feed_health.record("scheduler", True)


def test_healthz_is_open_and_touches_no_data(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert not it.DATA_DIR.exists() or not list(it.DATA_DIR.iterdir())


def test_readyz_is_open_and_ok_when_everything_is_in_place(client):
    _ready()
    r = client.get("/readyz")
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] is True and d["reasons"] == []
    assert set(d) == {"ok", "reasons", "commit", "time"}      # nothing else leaks


def test_readyz_before_any_inventory_exists_is_still_ready(client):
    feed_health.record("scheduler", True)
    assert client.get("/readyz").get_json()["ok"] is True


def test_readyz_reports_reason_codes(client, monkeypatch):
    _ready()
    it.save_inventory({}, allow_empty=True)
    d = client.get("/readyz").get_json()
    assert d["reasons"] == ["inventory_empty"]
    it._backup_path(it.INVENTORY_FILE).unlink()
    it.INVENTORY_FILE.write_text("{torn")
    it._FILE_CACHE.clear()
    r = client.get("/readyz")
    assert r.status_code == 503 and r.get_json()["reasons"] == ["inventory_unreadable"]
    assert "torn" not in r.get_data(as_text=True)


def test_readyz_scheduler_heartbeat(client, monkeypatch):
    it.save_inventory({"a": {"name": "A", "quantity": 1}})
    assert client.get("/readyz").get_json()["reasons"] == ["scheduler_no_heartbeat"]
    rec = feed_health._load()
    rec["feeds"]["scheduler"] = {"ok": True, "last_ok": time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))}
    feed_health._save(rec)
    assert client.get("/readyz").get_json()["reasons"] == ["scheduler_heartbeat_stale"]
    monkeypatch.setenv("INPROCESS_SCHEDULER", "off")
    assert client.get("/readyz").get_json()["ok"] is True


def test_readyz_lock_busy_and_disk_full(client, monkeypatch):
    import contextlib
    import shutil
    _ready()

    @contextlib.contextmanager
    def busy(blocking=True):
        yield False if not blocking else True
    monkeypatch.setattr(it, "data_lock", busy)
    assert client.get("/readyz").get_json()["reasons"] == ["data_lock_busy"]
    monkeypatch.undo()
    monkeypatch.setenv("INVENTORY_API_TOKEN", TOKEN)
    DU = collections.namedtuple("DU", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: DU(10**9, 10**9, 10**6))
    assert "disk_full" in client.get("/readyz").get_json()["reasons"]


def test_deep_health_needs_auth_and_has_the_details(client):
    _ready()
    it.save_usage([{"amount": 1}] * 3)
    assert client.get("/healthz/deep").status_code == 302      # -> login
    d = client.get("/healthz/deep", headers=H).get_json()
    assert d["ok"] is True and d["sku_count"] == 1 and d["usage_rows"] == 3
    assert d["files"]["inventory.json"]["bytes"] > 0
    assert d["data_lock_free"] is True and "backups" in d and "stale_feeds" in d
