"""Hidden SKUs stay off the Inventory tab (JD 2026-11-12: Pumpernickel's
October LTO is over and its 9 rows were distracting) but keep ingesting."""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

import inventory_tracker as it


def _item(name, wh):
    return {"name": name, "quantity": 10.0, "unit": "cs", "category": "bagels",
            "price": 1.0, "low_stock_threshold": 0, "distributor": "US Foods",
            "warehouse": wh, "on_order": [], "weekly_usage": 1.0}


def test_hidden_flag_round_trips_and_leaves_the_inventory_tab(monkeypatch):
    store = {"plain bagel 4oz [usf - zebulon]": _item("Plain Bagel 4oz [USF - Zebulon]", "Zebulon, NC"),
             "pumpernickel bagel 4oz [usf - zebulon]": _item("Pumpernickel Bagel 4oz [USF - Zebulon]", "Zebulon, NC")}
    monkeypatch.setattr(it, "load_inventory", lambda: store)
    monkeypatch.setattr(it, "save_inventory", lambda inv: None)
    it.update_item("Pumpernickel Bagel 4oz [USF - Zebulon]", hidden=True)
    assert store["pumpernickel bagel 4oz [usf - zebulon]"]["hidden"] is True
    assert store["pumpernickel bagel 4oz [usf - zebulon]"]["quantity"] == 10.0

    import blueprints.inventory as B
    from app import app
    monkeypatch.setattr(B, "load_inventory", lambda: store)
    monkeypatch.setattr(B, "_last_count_by_warehouse", lambda inv: {})
    with app.test_request_context("/api/distributors"):
        groups = B.api_distributors().get_json()
    names = [i["name"] for g in groups for w in g["warehouses"] for i in w["items"]]
    assert names == ["Plain Bagel 4oz [USF - Zebulon]"]
    with app.test_request_context("/api/distributors?include_hidden=1"):
        groups = B.api_distributors().get_json()
    assert sum(len(w["items"]) for g in groups for w in g["warehouses"]) == 2
    with app.test_request_context("/api/hidden-skus"):
        assert B.api_hidden_skus().get_json() == ["Pumpernickel Bagel 4oz [USF - Zebulon]"]

    it.update_item("Pumpernickel Bagel 4oz [USF - Zebulon]", hidden=False)
    assert "hidden" not in store["pumpernickel bagel 4oz [usf - zebulon]"]
