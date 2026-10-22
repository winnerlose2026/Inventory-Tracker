"""Activity-log Reverse must undo exactly the row the operator clicked.

/api/usage/reverse took the FIRST row with the clicked timestamp. Every scan,
rollover pass and daily burn stamps one `now` on all the rows it writes (live
2026-10-22: 21,461 rows over 424 timestamps), so clicking Reverse on Zebulon
Onion's burn credited Riviera Beach Plain instead -- and Onion could then never
be reversed. The row is now matched on timestamp + item_key + amount (+ PO),
refused when ambiguous, and PO / count rows are refused outright (Reopen /
Adjust on the Pending POs tab keep the PO and count consistent).
"""
from __future__ import annotations

from pathlib import Path

import pytest

TOKEN = "test-token-123"
H = {"X-Inventory-Token": TOKEN}
A = "plain bagel 4oz [cb - riviera beach]"
B = "onion bagel 4oz [usf - zebulon]"
TS = "2026-10-21T05:05:00.089033+00:00"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INVENTORY_API_TOKEN", TOKEN)
    import inventory_tracker as it
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    for attr, fn in (("INVENTORY_FILE", "inventory.json"), ("USAGE_FILE", "usage.json"),
                     ("PO_DELIVERY_FILE", "po_delivery.json")):
        monkeypatch.setattr(it, attr, tmp_path / fn)
    from seed_bagels import BAGELS
    inv = {b["name"].lower(): dict(b, on_order=[], unit="cs", low_stock_threshold=0)
           for b in BAGELS}
    inv[A]["quantity"] = 50.0
    inv[B]["quantity"] = 50.0
    it.save_inventory(inv)
    it.save_usage([
        {"item_key": A, "item_name": inv[A]["name"], "amount": 8.7, "unit": "cs",
         "timestamp": TS, "source": "forecast-daily", "forecast_date": "2026-10-21"},
        {"item_key": B, "item_name": inv[B]["name"], "amount": 0.45, "unit": "cs",
         "timestamp": TS, "source": "forecast-daily", "forecast_date": "2026-10-21"},
    ])
    import app as app_module
    app_module.app.config["TESTING"] = True
    return it, app_module.app.test_client()


def _rev(client, **body):
    return client.post("/api/usage/reverse", json=body, headers=H).get_json()


def test_reverses_the_clicked_row_not_the_first_with_that_timestamp(env):
    it, c = env
    r = _rev(c, timestamp=TS, item_key=B, amount=0.45)
    assert r["ok"] and r["item_name"].startswith("Onion")
    inv = it.load_inventory()
    assert inv[A]["quantity"] == 50.0 and inv[B]["quantity"] == 50.45
    usage = it.load_usage()
    assert not usage[0].get("reversed") and usage[1]["reversed"] is True
    # ...and the other row is still reachable.
    assert _rev(c, timestamp=TS, item_key=A, amount=8.7)["ok"]
    assert it.load_inventory()[A]["quantity"] == 58.7


def test_timestamp_alone_is_refused_when_shared(env):
    it, c = env
    r = _rev(c, timestamp=TS)
    assert r["ok"] is False and "refusing to guess" in r["error"]
    inv = it.load_inventory()
    assert inv[A]["quantity"] == 50.0 and inv[B]["quantity"] == 50.0
    assert not any(e.get("reversed") for e in it.load_usage())


def test_unique_timestamp_alone_still_works(env):
    it, c = env
    usage = it.load_usage()
    usage.append({"item_key": A, "amount": 2.0, "unit": "cs", "source": "manual-use",
                  "timestamp": "2026-10-21T15:00:00", "note": "spoiled"})
    it.save_usage(usage)
    assert _rev(c, timestamp="2026-10-21T15:00:00")["ok"]
    assert it.load_inventory()[A]["quantity"] == 52.0


@pytest.mark.parametrize("row,needle", [
    ({"source": "on_order_rollover", "amount": -56.0, "po_number": "7199415O"}, "Pending POs"),
    ({"source": "on_order_absorbed", "amount": 0, "po_number": "393072B2"}, "Pending POs"),
    ({"source": "arrived-po-adjust", "amount": 8.0, "po_number": "2753463T"}, "Pending POs"),
    ({"amount": 64.0, "po_number": "014511743693", "reversal_of_revision": ""}, "Pending POs"),
    ({"amount": -3.5, "note": "Email on-hand sync (subject: Weekly report)"}, "distributor count"),
])
def test_po_and_count_rows_are_refused(env, row, needle):
    it, c = env
    usage = it.load_usage()
    usage.append({"item_key": A, "unit": "cs", "timestamp": "2026-10-20T10:00:00", **row})
    it.save_usage(usage)
    r = _rev(c, timestamp="2026-10-20T10:00:00", item_key=A, amount=row["amount"],
             po_number=row.get("po_number", ""))
    assert r["ok"] is False and needle in r["error"]
    assert it.load_inventory()[A]["quantity"] == 50.0
    assert not it.load_usage()[-1].get("reversed")
