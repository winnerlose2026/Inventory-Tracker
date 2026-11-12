"""Storage layer: multi-file commit, journal recovery, .bak handling, NaN."""
import json
import os
import time

import pytest

import inventory_tracker as it
from core import feed_health


def _read(p):
    return json.loads(p.read_text())


def test_commit_state_writes_every_file_and_clears_the_journal():
    it.commit_state({it.INVENTORY_FILE: {"a": {"name": "A", "quantity": 1}},
                     it.USAGE_FILE: [{"item_key": "a", "amount": 1}]})
    assert _read(it.INVENTORY_FILE)["a"]["quantity"] == 1
    assert _read(it.USAGE_FILE)[0]["amount"] == 1
    assert not (it.DATA_DIR / ".txn").exists()
    assert not list(it.DATA_DIR.glob("*.tmp.*"))
    # cache signature matches what is on disk (taken from fstat, pre-rename)
    st = it.INVENTORY_FILE.stat()
    assert it._FILE_CACHE[str(it.INVENTORY_FILE)][0] == (st.st_mtime_ns, st.st_size)
    it.commit_state({it.INVENTORY_FILE: {"a": {"name": "A", "quantity": 2}},
                     it.USAGE_FILE: [{"item_key": "a", "amount": 1}] * 2})
    assert _read(it._backup_path(it.INVENTORY_FILE))["a"]["quantity"] == 1


def _stage(files):
    """Leave the disk as a process killed right after writing the journal."""
    entries = []
    for path, data in files.items():
        tmp, sig = it._dump_to_temp(path, data)
        entries.append({"dest": path.name, "tmp": tmp.name, "size": sig[1]})
    (it.DATA_DIR / ".txn").write_text(json.dumps({"files": entries}))
    return entries


def test_leftover_journal_is_rolled_forward_on_next_lock():
    it._save(it.INVENTORY_FILE, {"a": {"quantity": 1}})
    it._save(it.USAGE_FILE, [])
    _stage({it.INVENTORY_FILE: {"a": {"quantity": 9}},
            it.USAGE_FILE: [{"item_key": "a", "amount": -8}]})
    with it.data_lock():
        pass
    assert _read(it.INVENTORY_FILE)["a"]["quantity"] == 9
    assert _read(it.USAGE_FILE) == [{"item_key": "a", "amount": -8}]
    assert it._read_json(it.INVENTORY_FILE, {})["a"]["quantity"] == 9
    assert not (it.DATA_DIR / ".txn").exists()


def test_half_applied_journal_finishes_the_remaining_file():
    it._save(it.INVENTORY_FILE, {"a": {"quantity": 1}})
    it._save(it.USAGE_FILE, [])
    entries = _stage({it.INVENTORY_FILE: {"a": {"quantity": 9}},
                      it.USAGE_FILE: [{"amount": -8}]})
    os.replace(it.DATA_DIR / entries[0]["tmp"], it.INVENTORY_FILE)  # 1 of 2 done
    with it.data_lock():
        pass
    assert _read(it.USAGE_FILE) == [{"amount": -8}]


def test_journal_with_an_incomplete_temp_is_rolled_back():
    it._save(it.INVENTORY_FILE, {"a": {"quantity": 1}})
    it._save(it.USAGE_FILE, [])
    entries = _stage({it.INVENTORY_FILE: {"a": {"quantity": 9}},
                      it.USAGE_FILE: [{"amount": -8}]})
    (it.DATA_DIR / entries[1]["tmp"]).write_text('[{"amou')
    with it.data_lock():
        pass
    assert _read(it.INVENTORY_FILE)["a"]["quantity"] == 1
    assert _read(it.USAGE_FILE) == []
    assert not list(it.DATA_DIR.glob("*.tmp.*"))
    assert "data-commit-rolled-back" in feed_health.snapshot()["issues"]


def test_orphan_temps_older_than_an_hour_are_swept_once_at_boot():
    it.DATA_DIR.mkdir(exist_ok=True)
    old = it.DATA_DIR / "usage.json.tmp.abc"
    new = it.DATA_DIR / "usage.json.tmp.def"
    old.write_text("x"); new.write_text("y")
    t = time.time() - 7200
    os.utime(old, (t, t))
    it._reset_boot_state()
    with it.data_lock():
        pass
    assert not old.exists() and new.exists()


def test_corrupt_main_is_quarantined_and_bak_survives_the_next_write():
    it._save(it.INVENTORY_FILE, {"a": {"quantity": 1}})
    it._save(it.INVENTORY_FILE, {"a": {"quantity": 2}})      # .bak = qty 1
    it.INVENTORY_FILE.write_text('{"a": {"quan')              # torn
    it._FILE_CACHE.clear()
    got = it._read_json(it.INVENTORY_FILE, {})
    assert got["a"]["quantity"] == 1
    assert list(it.DATA_DIR.glob("inventory.json.corrupt-*"))
    assert _read(it.INVENTORY_FILE)["a"]["quantity"] == 1     # main healed
    assert "data-corrupt:inventory.json" in feed_health.snapshot()["issues"]
    it._save(it.INVENTORY_FILE, {"a": {"quantity": 3}})
    assert _read(it._backup_path(it.INVENTORY_FILE))["a"]["quantity"] == 1


def test_corrupt_main_never_overwrites_bak_even_when_not_healed(monkeypatch):
    it._save(it.INVENTORY_FILE, {"a": {"quantity": 1}})
    it._save(it.INVENTORY_FILE, {"a": {"quantity": 2}})
    it.INVENTORY_FILE.write_text("{nope")
    it._CORRUPT_MAIN.add(str(it.INVENTORY_FILE))             # lock was busy
    it._save(it.INVENTORY_FILE, {"a": {"quantity": 3}})
    assert _read(it._backup_path(it.INVENTORY_FILE))["a"]["quantity"] == 1
    assert str(it.INVENTORY_FILE) not in it._CORRUPT_MAIN


def test_nan_and_infinity_are_refused():
    it._save(it.INVENTORY_FILE, {"a": {"quantity": 1}})
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            it._save(it.INVENTORY_FILE, {"a": {"quantity": bad}})
    assert _read(it.INVENTORY_FILE)["a"]["quantity"] == 1
    assert not list(it.DATA_DIR.glob("*.tmp.*"))


def test_a_file_already_holding_nan_can_still_be_saved():
    it.DATA_DIR.mkdir(exist_ok=True)
    it.INVENTORY_FILE.write_text('{"a": {"quantity": NaN}}')
    it._save(it.INVENTORY_FILE, {"a": {"quantity": float("nan")}, "b": {}})
    assert "b" in it.INVENTORY_FILE.read_text()
    assert "data-nonfinite:inventory.json" in feed_health.snapshot()["issues"]


def test_labor_goes_through_the_atomic_writer():
    it.save_labor([{"date": "2026-11-01", "hours": 8}])
    it.save_labor([{"date": "2026-11-01", "hours": 9}])
    assert it.load_labor()[0]["hours"] == 9
    assert _read(it._backup_path(it.LABOR_FILE))[0]["hours"] == 8
    it._backup_path(it.LABOR_FILE).unlink()
    it.LABOR_FILE.write_text("[{")
    it._FILE_CACHE.clear()
    with pytest.raises(it.DataFileCorrupt):
        it.load_labor()


def test_save_inventory_and_usage_is_one_commit(monkeypatch):
    it._save(it.INVENTORY_FILE, {"a": {"quantity": 1}})
    it._save(it.USAGE_FILE, [])
    real = it._dump_to_temp
    calls = []

    def boom(path, data):
        calls.append(path.name)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(path, data)
    monkeypatch.setattr(it, "_dump_to_temp", boom)
    with pytest.raises(OSError):
        it.save_inventory_and_usage({"a": {"quantity": 5}}, [{"amount": -4}])
    assert _read(it.INVENTORY_FILE)["a"]["quantity"] == 1     # neither landed
    assert _read(it.USAGE_FILE) == []
    assert not list(it.DATA_DIR.glob("*.tmp.*"))
