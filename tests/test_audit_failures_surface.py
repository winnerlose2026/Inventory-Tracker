"""Audit / activity-log write failures are reported, and failed maintenance
calls answer 500 instead of 200."""
import pytest

import inventory_tracker as it
from core import activity, feed_health, util

TOKEN = "tok"
H = {"X-Inventory-Token": TOKEN}


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    util._AUDIT_FAIL_LAST.clear()


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKEN", TOKEN)
    monkeypatch.delenv("INVENTORY_ADMINS", raising=False)
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def _boom(*a, **k):
    raise OSError("disk full")


def test_failed_audit_append_raises_an_issue_and_counts(monkeypatch):
    monkeypatch.setattr(it, "append_inventory_audit", _boom)
    assert util.append_audit_safely([{"x": 1}], "admin-remove-po") is False
    snap = feed_health.snapshot()
    assert "audit-write-failed" in snap["issues"]
    assert snap["feeds"]["audit-write"]["ok"] is False
    assert snap["feeds"]["audit-write"]["failures"] == 1
    # rate-limited per call site, counted across sites
    util.append_audit_safely([{"x": 1}], "admin-remove-po")
    util.append_audit_safely([{"x": 1}], "admin-uncancel-po")
    assert feed_health.snapshot()["feeds"]["audit-write"]["failures"] == 2


def test_successful_audit_append_reports_nothing():
    assert util.append_audit_safely([{"ts": "t", "name": "n"}], "x") is True
    assert feed_health.snapshot()["issues"] == {}
    assert it.load_inventory_audit()[0]["name"] == "n"


def test_remove_po_still_succeeds_when_its_audit_write_fails(c, monkeypatch):
    it.save_inventory({"a": {"name": "A", "quantity": 1, "on_order": [
        {"po_number": "P1", "qty": 5}]}})
    monkeypatch.setattr(it, "append_inventory_audit", _boom)
    r = c.post("/api/admin/remove-po", json={"po_number": "P1"}, headers=H)
    assert r.status_code == 200 and r.get_json()["removed_entries"] == 1
    assert "audit-write-failed" in feed_health.snapshot()["issues"]


def test_activity_log_failure_is_reported(monkeypatch):
    monkeypatch.setattr(activity, "_log_path", _boom)
    activity.record({"x": 1})                       # must not raise
    assert "audit-write-failed" in feed_health.snapshot()["issues"]


def test_seed_and_migrate_units_answer_500_on_failure(c, monkeypatch):
    import seed_bagels
    monkeypatch.setattr(seed_bagels, "seed", _boom)
    r = c.post("/api/seed", json={}, headers=H)
    assert r.status_code == 500 and r.get_json()["ok"] is False
    it.save_inventory({"a": {"name": "A", "quantity": 1}})
    monkeypatch.setattr(it, "migrate_units_to_case", _boom)
    r = c.post("/api/migrate-units", json={}, headers=H)
    assert r.status_code == 500 and r.get_json()["ok"] is False
