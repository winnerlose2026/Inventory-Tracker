"""Toast sales in SQLite (integrations/sales_store.py), 11/16/2026.

sales.json had reached 52 MB / ~169k rows; every reader parsed it whole and
the 512 MB web instance ran out of memory nightly. Verified on the real file
before shipping: 168,970 rows round-trip unchanged, import peak 46 MB, every
Report / Planner number identical.
"""
import json
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

import inventory_tracker as it
from ingest_fakes import isolate
from integrations import sales_store as S

UES = "13339585-284c-40cd-8a9b-01789ea875e6"
MTH = "dd987834-83cf-4960-b161-5482ee99e8e1"


def _row(guid, day, item_guid, qty, item="Plain Bagel", **kw):
    r = {"restaurant_guid": guid, "location": "UES" if guid == UES else "MTH",
         "business_date": day, "item_guid": item_guid, "item": item,
         "menu_group": "Bagels", "qty": qty, "gross": qty * 2.5, "net": qty * 2.4}
    r.update(kw)
    return r


def test_legacy_json_is_imported_once_and_kept_as_migrated(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    rows = [_row(UES, "2026-11-01", "a", 3), _row(MTH, "2026-11-02", "b", 4, extra_key="x")]
    (tmp_path / "sales.json").write_text(json.dumps(rows, indent=2))
    assert it.load_sales() == rows                       # every field, extras too
    assert not (tmp_path / "sales.json").exists()
    assert (tmp_path / "sales.json.migrated").exists()
    S._ready.clear()                                      # a fresh process: no re-import
    assert S.count() == 2


def test_windowed_reads_and_store_filter(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    it.save_sales([_row(UES, f"2026-11-{d:02d}", "a", d) for d in range(1, 11)]
                  + [_row(MTH, "2026-11-05", "a", 1)])
    got = it.load_sales(since="2026-11-04", until="2026-11-06")
    assert [(r["business_date"], r["restaurant_guid"]) for r in got] == [
        ("2026-11-04", UES), ("2026-11-05", UES), ("2026-11-05", MTH), ("2026-11-06", UES)]
    assert len(it.load_sales(restaurant_guid=MTH)) == 1
    assert S.pairs(since="2026-11-09") == {(UES, "2026-11-09"), (UES, "2026-11-10")}
    assert S.latest_date() == "2026-11-10" and S.dates()[:2] == ["2026-11-10", "2026-11-09"]
    loc = {s["restaurant_guid"]: s for s in S.location_summary()}
    assert loc[UES]["rows"] == 10 and loc[UES]["min_date"] == "2026-11-01"


def test_a_pull_replaces_whole_days_only(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    it.save_sales([_row(UES, "2026-11-01", "a", 1), _row(UES, "2026-11-01", "gone", 9),
                   _row(UES, "2026-11-02", "a", 2)])
    S.replace_days([_row(UES, "2026-11-01", "a", 5)])
    got = {(r["business_date"], r["item_guid"]): r["qty"] for r in it.load_sales()}
    assert got == {("2026-11-01", "a"): 5, ("2026-11-02", "a"): 2}


def test_ingest_route_upserts_and_counts(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    from app import app
    c = app.test_client()
    h = {"X-Inventory-Token": "tok"}
    r = c.post("/api/sales/ingest", json={"rows": [_row(UES, "2026-11-01", "a", 1),
                                                   _row(UES, "2026-11-01", "b", 2)]},
               headers=h).get_json()
    assert (r["added"], r["updated"], r["total_rows"]) == (2, 0, 2)
    r = c.post("/api/sales/ingest", json={"rows": [_row(UES, "2026-11-01", "a", 7)]},
               headers=h).get_json()
    assert (r["added"], r["updated"], r["total_rows"]) == (0, 1, 2)
    r = c.post("/api/sales/ingest", json={"rows": [_row(UES, "2026-11-01", "c", 1)],
                                          "replace_dates": True}, headers=h).get_json()
    assert r["total_rows"] == 1


def test_report_reads_the_most_recent_buckets_with_data(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    # data in two weeks with a month-long gap between them
    it.save_sales([_row(UES, "2026-11-10", "a", 3, item="Bagel W/ Cream Cheese"),
                   _row(UES, "2026-11-11", "b", 4, item="Bagel w/ Cream Cheese"),
                   _row(UES, "2026-10-01", "a", 5)])
    from app import app
    j = app.test_client().get("/api/report/toast-sales?period=week&buckets=2",
                              headers={"X-Inventory-Token": "tok"}).get_json()
    assert [b["key"] for b in j["buckets"]] == ["2026-11-09", "2026-09-28"]
    top = j["buckets"][0]["items"][0]
    assert top["qty"] == 7 and top["item"] == "Bagel w/ Cream Cheese"   # newest spelling


def test_backup_and_restore_carry_the_store(tmp_path, monkeypatch):
    import io
    import tarfile
    from integrations import backups as B
    isolate(tmp_path, monkeypatch)
    it.save_sales([_row(UES, "2026-11-01", "a", 1), _row(UES, "2026-11-02", "a", 2)])
    snap = B.make_snapshot("test")
    assert snap["ok"] and snap["files"]["sales.sqlite3"]["rows"] == 2
    it.save_sales([_row(UES, "2026-11-03", "z", 9)])
    dry = B.restore(snap["name"])
    assert dry["files"]["sales.sqlite3"]["action"] == "replace"
    assert "sales.sqlite3" in dry["would_write"] and S.count() == 1
    done = B.restore(snap["name"], dry_run=False)
    assert "sales.sqlite3" in done["written"]
    assert {r["business_date"] for r in it.load_sales()} == {"2026-11-01", "2026-11-02"}
    assert B.restore(snap["name"])["files"]["sales.sqlite3"]["action"] == "unchanged"


def test_restoring_a_pre_sqlite_snapshot_imports_its_sales_json(tmp_path, monkeypatch):
    import io
    import tarfile
    from integrations import backups as B
    isolate(tmp_path, monkeypatch)
    it.save_sales([_row(UES, "2026-11-03", "z", 9)])
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, doc in (("sales.json", [_row(MTH, "2026-10-01", "a", 4)]),
                          ("inventory.json", {})):
            data = json.dumps(doc).encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    up = B.store_upload(raw.getvalue(), "20261001T000000Z-hourly.tar.gz")
    done = B.restore(up["name"], dry_run=False)
    assert done["ok"], done
    assert [r["restaurant_guid"] for r in it.load_sales()] == [MTH]
    assert not (tmp_path / "sales.json").exists()        # not written as a file


def test_the_pre_import_copies_expire_after_two_weeks(tmp_path, monkeypatch):
    import os
    import time
    isolate(tmp_path, monkeypatch)
    (tmp_path / "sales.json").write_text(json.dumps([_row(UES, "2026-11-01", "a", 1)]))
    (tmp_path / "sales.json.bak").write_text("[]")
    S.count()
    mig = tmp_path / "sales.json.migrated"
    assert mig.exists() and S._expire_legacy() == []          # fresh: kept
    old = time.time() - 15 * 86400
    os.utime(mig, (old, old))
    assert sorted(S._expire_legacy()) == ["sales.json.bak", "sales.json.migrated"]
    assert not mig.exists() and S.count() == 1
