"""The manual Add / Edit / Use / Restock / Delete endpoints tell the truth."""
import pytest

import inventory_tracker as it

TOKEN = "tok"
H = {"X-Inventory-Token": TOKEN}
NAME = "Plain Bagel 4oz [USF - Zebulon]"
KEY = NAME.lower()


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKEN", TOKEN)
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    it.save_inventory({KEY: {"name": NAME, "quantity": 10.0, "unit": "cs",
                             "category": "bagels", "price": 1.0,
                             "low_stock_threshold": 0, "warehouse": "Zebulon, NC",
                             "on_order": [], "weekly_usage": 1.0,
                             "updated": "2026-11-01T00:00:00"}})
    it.save_usage([])
    return app_mod.app.test_client()


def _qty():
    return it._read_json(it.INVENTORY_FILE, {})[KEY]["quantity"]


def test_use_more_than_on_hand_is_refused(c):
    r = c.post("/api/use", json={"name": NAME, "amount": 11}, headers=H)
    d = r.get_json()
    assert r.status_code == 409 and d["ok"] is False and "Only 10" in d["error"]
    assert _qty() == 10 and it.load_usage() == []


def test_use_and_restock_work_and_write_one_ledger_row_each(c):
    d = c.post("/api/use", json={"name": NAME, "amount": 4, "note": "samples"},
               headers=H).get_json()
    assert d["ok"] is True and d["quantity"] == 6
    d = c.post("/api/restock", json={"name": NAME, "amount": 2}, headers=H).get_json()
    assert d["ok"] is True and d["quantity"] == 8
    rows = it.load_usage()
    assert [(r["amount"], r["source"]) for r in rows] == [
        (4, "manual-use"), (-2, "manual-restock")]
    assert not (it.DATA_DIR / ".txn").exists()


def test_unknown_sku_is_404_everywhere(c):
    for method, url, body in (
            ("post", "/api/use", {"name": "nope", "amount": 1}),
            ("post", "/api/restock", {"name": "nope", "amount": 1}),
            ("put", "/api/inventory/nope", {"price": 2}),
            ("delete", "/api/inventory/nope", None)):
        r = getattr(c, method)(url, json=body, headers=H)
        assert r.status_code == 404, url
        assert r.get_json()["ok"] is False and "not found" in r.get_json()["error"]
    assert it.load_usage() == []


def test_duplicate_add_is_409_and_delete_reports_success(c):
    body = {"name": NAME.upper(), "quantity": 1, "unit": "cs"}
    r = c.post("/api/inventory", json=body, headers=H)
    assert r.status_code == 409 and r.get_json()["ok"] is False
    assert _qty() == 10
    r = c.post("/api/inventory", json={"name": "New SKU", "quantity": 3,
                                       "unit": "cs"}, headers=H)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    r = c.delete("/api/inventory/New SKU", headers=H)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert "new sku" not in it._read_json(it.INVENTORY_FILE, {})


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", -3, 0,
                                    "abc", None, True])
def test_bad_amounts_are_400(c, amount):
    for url in ("/api/use", "/api/restock"):
        r = c.post(url, data='{"name": "%s", "amount": %s}' % (
            NAME, amount if isinstance(amount, str) and amount != "abc"
            else __import__("json").dumps(amount)),
            headers={**H, "Content-Type": "application/json"})
        assert r.status_code == 400 and r.get_json()["ok"] is False, (url, amount)
    assert _qty() == 10 and it.load_usage() == []


def test_bad_bodies_are_400_not_500(c):
    assert c.post("/api/use", json={"amount": 1}, headers=H).status_code == 400
    assert c.post("/api/use", data="not json", headers=H).status_code == 400
    assert c.post("/api/inventory", json={"name": "X"}, headers=H).status_code == 400
    r = c.put(f"/api/inventory/{NAME}", data='{"quantity": NaN}',
              headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 400 and _qty() == 10
    r = c.put(f"/api/inventory/{NAME}", json={"price": -1}, headers=H)
    assert r.status_code == 400


def test_request_id_makes_use_and_restock_idempotent(c):
    body = {"name": NAME, "amount": 3, "request_id": "abc-123"}
    first = c.post("/api/use", json=body, headers=H).get_json()
    again = c.post("/api/use", json=body, headers=H).get_json()
    assert first["quantity"] == 7 and "duplicate" not in first
    assert again == {"ok": True, "duplicate": True, "name": NAME,
                     "amount": 3, "quantity": 7}
    assert _qty() == 7 and len(it.load_usage()) == 1
    assert it.load_usage()[0]["request_id"] == "abc-123"
    body = {"name": NAME, "amount": 5, "request_id": "r-2"}
    c.post("/api/restock", json=body, headers=H)
    again = c.post("/api/restock", json=body, headers=H).get_json()
    assert again["duplicate"] is True and again["quantity"] == 12 and _qty() == 12
    # without an id every call counts
    c.post("/api/use", json={"name": NAME, "amount": 1}, headers=H)
    c.post("/api/use", json={"name": NAME, "amount": 1}, headers=H)
    assert _qty() == 10


def test_put_without_quantity_leaves_on_hand_and_the_ledger_alone(c):
    r = c.put(f"/api/inventory/{NAME}", json={"price": 2.5, "hidden": True},
              headers=H)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    item = it._read_json(it.INVENTORY_FILE, {})[KEY]
    assert item["quantity"] == 10 and item["price"] == 2.5 and item["hidden"] is True
    assert it.load_usage() == []
    # same quantity sent back: still no correction row
    c.put(f"/api/inventory/{NAME}", json={"quantity": 10}, headers=H)
    assert it.load_usage() == []
    # a real change writes exactly one, in the same commit
    d = c.put(f"/api/inventory/{NAME}", json={"quantity": 8}, headers=H).get_json()
    assert d["correction"] == 2
    rows = it.load_usage()
    assert len(rows) == 1 and rows[0]["source"] == "manual-correction"


def test_put_with_a_stale_expected_updated_is_409_with_the_current_item(c):
    r = c.put(f"/api/inventory/{NAME}",
              json={"quantity": 3, "expected_updated": "2026-10-01T00:00:00"},
              headers=H)
    d = r.get_json()
    assert r.status_code == 409 and d["ok"] is False and d["error"] == "changed"
    assert d["current"]["quantity"] == 10 and d["current"]["name"] == NAME
    assert _qty() == 10 and it.load_usage() == []
    r = c.put(f"/api/inventory/{NAME}",
              json={"quantity": 3, "expected_updated": "2026-11-01T00:00:00"},
              headers=H)
    assert r.status_code == 200 and _qty() == 3
    # and the stamp moved, so the same edit sent again is now stale
    r = c.put(f"/api/inventory/{NAME}",
              json={"quantity": 4, "expected_updated": "2026-11-01T00:00:00"},
              headers=H)
    assert r.status_code == 409


def test_helpers_return_results_for_non_http_callers():
    it.save_inventory({"a": {"name": "A", "quantity": 1, "unit": "cs"}})
    assert it.record_usage("A", 5)["code"] == "insufficient"
    assert it.restock("zzz", 1)["code"] == "not_found"
    assert it.restock("A", float("nan"))["code"] == "invalid"
    assert it.add_item("A", 1, "cs")["code"] == "exists"
    assert it.remove_item("zzz")["code"] == "not_found"
    assert it.update_item("zzz", price=1)["code"] == "not_found"
    assert it.restock("A", 2)["quantity"] == 3
