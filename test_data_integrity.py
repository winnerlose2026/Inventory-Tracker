"""Guards against the 2026-09-17 inventory wipe.

That incident had three links in the chain, and each one is pinned here:
  1. a non-atomic write let two concurrent workers tear inventory.json,
  2. a bare except in _read_json reported the torn file as an empty {},
  3. save_inventory wrote that {} back, making the loss permanent.

pytest or standalone.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
import inventory_tracker as it


def _sandbox(tmp):
    """Point the module's data files at a scratch dir and drop the cache."""
    d = Path(tmp)
    it.DATA_DIR = d
    it.INVENTORY_FILE = d / "inventory.json"
    it.USAGE_FILE = d / "usage.json"
    it._FILE_CACHE.clear()
    return d


ITEMS = {
    "plain bagel 4oz [usf - zebulon]": {
        "name": "Plain Bagel 4oz [USF - Zebulon]", "quantity": 42.0,
        "warehouse": "Zebulon, NC", "on_order": [{"po_number": "P1", "qty": 8}],
    },
    "sesame bagel 4oz [cb - ocala]": {
        "name": "Sesame Bagel 4oz [CB - Ocala]", "quantity": 17.0,
        "warehouse": "Ocala, FL", "on_order": [],
    },
}


def test_missing_file_is_still_an_empty_default():
    """A genuinely absent file is a first run, not corruption."""
    with tempfile.TemporaryDirectory() as tmp:
        d = _sandbox(tmp)
        assert it._read_json(d / "nope.json", {}) == {}
        assert it._read_json(d / "nope.json", []) == []


def test_corrupt_file_raises_instead_of_reading_empty():
    """The bug: a truncated file used to come back as {}."""
    with tempfile.TemporaryDirectory() as tmp:
        d = _sandbox(tmp)
        it.INVENTORY_FILE.write_text('{"plain bagel 4oz [usf - zeb', encoding="utf-8")
        try:
            it._read_json(it.INVENTORY_FILE, {})
        except it.DataFileCorrupt as exc:
            assert "not valid JSON" in str(exc)
        else:
            raise AssertionError("a torn inventory.json must not read as empty")


def test_corrupt_file_recovers_from_the_rolling_backup():
    with tempfile.TemporaryDirectory() as tmp:
        d = _sandbox(tmp)
        it._write_json(it.INVENTORY_FILE, ITEMS)          # writes the file
        it._write_json(it.INVENTORY_FILE, ITEMS)          # now .bak exists too
        it.INVENTORY_FILE.write_text("{ truncated", encoding="utf-8")
        it._FILE_CACHE.clear()
        got = it._read_json(it.INVENTORY_FILE, {})
        assert set(got) == set(ITEMS)


def test_write_is_atomic_and_keeps_a_backup():
    with tempfile.TemporaryDirectory() as tmp:
        d = _sandbox(tmp)
        it._write_json(it.INVENTORY_FILE, ITEMS)
        newer = dict(ITEMS)
        newer["plain bagel 4oz [usf - zebulon]"] = dict(
            ITEMS["plain bagel 4oz [usf - zebulon]"], quantity=99.0)
        it._write_json(it.INVENTORY_FILE, newer)
        bak = json.loads((d / "inventory.json.bak").read_text(encoding="utf-8"))
        assert bak["plain bagel 4oz [usf - zebulon]"]["quantity"] == 42.0
        live = json.loads(it.INVENTORY_FILE.read_text(encoding="utf-8"))
        assert live["plain bagel 4oz [usf - zebulon]"]["quantity"] == 99.0
        # no temp files left behind
        assert not list(d.glob("*.tmp.*"))


def test_save_refuses_to_blank_a_populated_inventory():
    """The amplifier: every scan ends in save_inventory()."""
    with tempfile.TemporaryDirectory() as tmp:
        _sandbox(tmp)
        it.save_inventory(ITEMS)
        try:
            it.save_inventory({})
        except ValueError as exc:
            assert "2 SKUs" in str(exc)
        else:
            raise AssertionError("an empty save over real data must be refused")
        assert len(json.loads(it.INVENTORY_FILE.read_text(encoding="utf-8"))) == 2


def test_save_refuses_to_blank_while_the_file_is_unreadable():
    """The exact 2026-09-17 sequence: corrupt read -> {} -> save."""
    with tempfile.TemporaryDirectory() as tmp:
        _sandbox(tmp)
        it.INVENTORY_FILE.write_text("{ torn", encoding="utf-8")
        it._FILE_CACHE.clear()
        try:
            it.save_inventory({})
        except ValueError as exc:
            assert "unreadable" in str(exc)
        else:
            raise AssertionError("must not blank an inventory it cannot read")


def test_deliberate_reset_is_still_possible():
    """seed_bagels(reset=True) has to be able to clear the file."""
    with tempfile.TemporaryDirectory() as tmp:
        _sandbox(tmp)
        it.save_inventory(ITEMS)
        it.save_inventory({}, allow_empty=True)
        assert json.loads(it.INVENTORY_FILE.read_text(encoding="utf-8")) == {}


def test_empty_save_is_fine_on_a_fresh_install():
    with tempfile.TemporaryDirectory() as tmp:
        _sandbox(tmp)
        it.save_inventory({})
        assert json.loads(it.INVENTORY_FILE.read_text(encoding="utf-8")) == {}


if __name__ == "__main__":
    ns = dict(globals())
    fns = [v for k, v in ns.items() if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print("%d passed" % len(fns))
