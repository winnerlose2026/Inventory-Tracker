"""Per-SKU count gaps on /api/scan/health (review 2026-10-22).

Warehouse freshness is the NEWEST count across a warehouse's SKUs, so a SKU no
weekly report lists looked as fresh as the rest: on 2026-10-22 Pumpernickel at
Manassas and at Chicago had sat at 20 cs since 8/28, never counted, under
"Counted Oct 19". Also: the mailbox-image routes compare the token in constant
time."""
import sys
from pathlib import Path

sys.path.insert(0, ".")

import inventory_tracker as it
from blueprints.email import _sku_count_gaps


def _item(name, wh, dist, lc, qty=10.0):
    return {"name": name, "warehouse": wh, "distributor": dist,
            "last_count_at": lc, "quantity": qty}


INV = {
    "plain bagel 4oz [usf - manassas]": _item(
        "Plain Bagel 4oz [USF - Manassas]", "Manassas, VA", "US Foods",
        "2026-10-19T13:16:22Z"),
    "pumpernickel bagel 4oz [usf - manassas]": _item(
        "Pumpernickel Bagel 4oz [USF - Manassas]", "Manassas, VA", "US Foods", "", 20.0),
    "egg bagel 4oz [usf - manassas]": _item(
        "Egg Bagel 4oz [USF - Manassas]", "Manassas, VA", "US Foods",
        "2026-10-05T13:00:00Z"),
    "onion bagel 4oz [usf - manassas]": _item(       # 4 days older: fine
        "Onion Bagel 4oz [USF - Manassas]", "Manassas, VA", "US Foods",
        "2026-10-15T13:00:00Z"),
    "plain bagel 4oz [usf - houston]": _item(        # whole warehouse uncounted
        "Plain Bagel 4oz [USF - Houston]", "Houston, TX", "US Foods", ""),
}


def test_gaps_are_never_counted_or_stale_against_the_warehouse():
    gaps = {g["sku"]: g for g in _sku_count_gaps(INV, 7)}
    assert set(gaps) == {"pumpernickel bagel 4oz [usf - manassas]",
                         "egg bagel 4oz [usf - manassas]"}
    assert gaps["pumpernickel bagel 4oz [usf - manassas]"]["reason"] == "never counted"
    assert gaps["pumpernickel bagel 4oz [usf - manassas]"]["quantity"] == 20.0
    assert "14 days before" in gaps["egg bagel 4oz [usf - manassas]"]["reason"]


def _isolate(tmp: Path, monkeypatch):
    old = it.DATA_DIR
    monkeypatch.setattr(it, "DATA_DIR", tmp)
    for name in dir(it):
        v = getattr(it, name)
        # Earlier tests re-point it.DATA_DIR without restoring it, so match
        # the *_FILE constants by name as well as by directory.
        if isinstance(v, Path) and (v.parent == old or name.endswith("_FILE")):
            monkeypatch.setattr(it, name, tmp / v.name)


def test_scan_health_lists_the_gaps(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    it._write_json(tmp_path / "inventory.json", INV)
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    from app import app
    d = app.test_client().get("/api/scan/health",
                              headers={"X-Inventory-Token": "tok"}).get_json()
    assert d["sku_count_gap_count"] == 2
    assert {g["name"] for g in d["sku_count_gaps"]} == {
        "Pumpernickel Bagel 4oz [USF - Manassas]", "Egg Bagel 4oz [USF - Manassas]"}


def test_mailbox_image_routes_still_require_the_exact_token(monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    monkeypatch.setenv("MS365_USER", "")
    from app import app
    c = app.test_client()
    for url in ("/api/email/cheney-stock-images", "/api/email/usf-screenshots"):
        assert c.get(url, headers={"X-Inventory-Token": "tok-x"}).status_code == 401
        assert c.get(url, headers={"X-Inventory-Token": "tok"}).status_code == 200
