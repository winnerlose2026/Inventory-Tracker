"""save_inventory / save_usage refuse a large unexplained shrink, and
/api/admin/restore-inventory is a dry run unless told otherwise."""
import pytest

import inventory_tracker as it
from core import feed_health

TOKEN = "tok"
H = {"X-Inventory-Token": TOKEN}


def _inv(n):
    return {f"sku{i}": {"name": f"SKU{i}", "quantity": i} for i in range(n)}


def test_inventory_shrink_of_more_than_ten_percent_is_refused():
    it.save_inventory(_inv(100))
    it.save_inventory(_inv(90))                      # exactly 10%: allowed
    with pytest.raises(it.ShrinkRefused):
        it.save_inventory(_inv(70))
    assert len(it._read_json(it.INVENTORY_FILE, {})) == 90
    assert "save-refused:inventory" in feed_health.snapshot()["issues"]
    it.save_inventory(_inv(70), allow_shrink=True)
    assert len(it._read_json(it.INVENTORY_FILE, {})) == 70


def test_small_inventories_may_lose_up_to_three_skus():
    it.save_inventory(_inv(8))
    it.save_inventory(_inv(5))                       # 3 SKUs: allowed
    with pytest.raises(it.ShrinkRefused):
        it.save_inventory(_inv(1))


def test_empty_inventory_still_needs_allow_empty():
    it.save_inventory(_inv(2))
    with pytest.raises(ValueError):
        it.save_inventory({})
    it.save_inventory({}, allow_empty=True)


def test_usage_shrink_of_more_than_two_percent_is_refused():
    rows = [{"item_key": "a", "amount": 1, "n": i} for i in range(2000)]
    it.save_usage(rows)
    it.save_usage(rows[:1960])                       # 2%: allowed
    with pytest.raises(it.ShrinkRefused):
        it.save_usage(rows[:1900])
    it.save_usage(rows[:1945])                       # 15 rows (<20): allowed
    assert len(it.load_usage()) == 1945
    it.save_usage(rows[:10], allow_shrink=True)
    assert len(it.load_usage()) == 10


def test_combined_save_applies_both_guards():
    it.save_inventory(_inv(50))
    it.save_usage([{"amount": 1}] * 500)
    with pytest.raises(it.ShrinkRefused):
        it.save_inventory_and_usage(_inv(50), [{"amount": 1}] * 100)
    with pytest.raises(it.ShrinkRefused):
        it.save_inventory_and_usage(_inv(10), [{"amount": 1}] * 500)
    assert len(it.load_usage()) == 500


def test_remove_item_passes_the_guard():
    it.save_inventory(_inv(4))
    for i in range(4):
        it.remove_item(f"SKU{i}") if i < 3 else None
    assert list(it._read_json(it.INVENTORY_FILE, {})) == ["sku3"]


# --- /api/admin/restore-inventory -----------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKEN", TOKEN)
    monkeypatch.delenv("INVENTORY_ADMINS", raising=False)
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _items(n):
    return [dict(v, on_order_qty=3) for v in _inv(n).values()]


def test_restore_inventory_defaults_to_a_dry_run(client):
    it.save_inventory(_inv(10))
    r = client.post("/api/admin/restore-inventory", json={"items": _items(4)},
                    headers=H).get_json()
    assert r["ok"] and r["dry_run"] is True
    assert r["dropped"] == 6 and r["needs_confirm"] is True
    assert len(it._read_json(it.INVENTORY_FILE, {})) == 10


def test_restore_inventory_needs_confirm_to_drop_and_snapshots_first(client):
    from integrations import backups
    it.save_inventory(_inv(10))
    r = client.post("/api/admin/restore-inventory",
                    json={"items": _items(4), "dry_run": False}, headers=H)
    assert r.status_code == 409 and "confirm" in r.get_json()["error"]
    assert len(it._read_json(it.INVENTORY_FILE, {})) == 10
    r = client.post("/api/admin/restore-inventory",
                    json={"items": _items(4), "dry_run": False, "confirm": True},
                    headers=H).get_json()
    assert r["ok"] and r["after_sku_count"] == 4
    stored = it._read_json(it.INVENTORY_FILE, {})
    assert len(stored) == 4 and "on_order_qty" not in stored["sku1"]
    assert r["snapshot"].endswith("-pre-restore.tar.gz")
    assert b'"sku9"' in backups.read_snapshot(r["snapshot"])["inventory.json"]


def test_restore_inventory_without_drops_needs_no_confirm(client):
    it.save_inventory(_inv(3))
    r = client.post("/api/admin/restore-inventory",
                    json={"items": _items(5), "dry_run": False}, headers=H).get_json()
    assert r["ok"] and r["added"] == 2 and r["dropped"] == 0


def test_restore_inventory_reports_an_unreadable_current_file(client):
    it.save_inventory(_inv(3))
    it._backup_path(it.INVENTORY_FILE).unlink(missing_ok=True)
    it.INVENTORY_FILE.write_text('{"torn')
    it._FILE_CACHE.clear()
    r = client.post("/api/admin/restore-inventory", json={"items": _items(3)},
                    headers=H).get_json()
    assert r["current_unreadable"] is True and "unreadable" in r["current_error"]
    r = client.post("/api/admin/restore-inventory",
                    json={"items": _items(3), "mode": "fill-missing"}, headers=H)
    assert r.status_code == 409
    r = client.post("/api/admin/restore-inventory",
                    json={"items": _items(3), "dry_run": False}, headers=H).get_json()
    assert r["ok"] and len(it._read_json(it.INVENTORY_FILE, {})) == 3


def test_restore_inventory_refuses_when_snapshot_fails(client, monkeypatch):
    from integrations import backups
    it.save_inventory(_inv(3))
    monkeypatch.setattr(backups, "free_bytes", lambda: 1)
    r = client.post("/api/admin/restore-inventory",
                    json={"items": _items(5), "dry_run": False}, headers=H)
    assert r.status_code == 500 and "nothing was written" in r.get_json()["error"]
    assert len(it._read_json(it.INVENTORY_FILE, {})) == 3
