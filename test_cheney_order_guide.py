"""Tests for Cheney's daily OrderGuide CSV + the guards that keep it out of the
on-hand path. Runs standalone or under pytest.

Fixtures in tests/fixtures/cheney/ are Cheney's real 2026-08-04 sample drop
(the Altamonte Springs file is a 30-row excerpt; the Chapel Hill file is whole).
"""
import sys
from pathlib import Path

sys.path.insert(0, ".")
from integrations.cheney_order_guide import (  # noqa: E402
    parse_order_guide, looks_like_order_guide, summarize,
    account_from_filename, snapshot_from_filename,
)
from integrations.cheney_csv_inventory import parse_inventory_csv  # noqa: E402
from integrations.cheney_dcs import (  # noqa: E402
    normalize_dc_code, warehouse_from_dc_code, is_known_dc, store_from_account,
)

FIX = Path(__file__).parent / "tests" / "fixtures" / "cheney"
ALTAMONTE = FIX / "OrderGuide-20260803-113324_0060458212.excerpt.csv"
CHAPEL_HILL = FIX / "OrderGuide-20260803-113324_0060415887.csv"

_HH_VARIETIES = {
    "Plain", "Poppy Seed", "Onion", "Sesame", "Whole Wheat", "Asiago",
    "Blueberry", "Jalapeno Cheddar", "Cinnamon Raisin", "Egg", "Everything",
    "Whole Wheat Everything",
}


def test_dc_and_account_crosswalks():
    assert normalize_dc_code("05") == "3005"
    assert normalize_dc_code("3005") == "3005"
    assert normalize_dc_code("5") == "3005"
    assert normalize_dc_code("") == ""
    assert warehouse_from_dc_code("01") == "Riviera Beach, FL"
    assert warehouse_from_dc_code("05") == "Ocala, FL"
    assert warehouse_from_dc_code("06") == "Punta Gorda, FL"
    # Statesville is a real Cheney DC we do NOT track inventory at: known, but
    # deliberately no warehouse label.
    assert is_known_dc("12") and warehouse_from_dc_code("12") == ""
    assert not is_known_dc("77")
    assert store_from_account("60446046") == "H&H Bagels Mandarin"
    assert store_from_account("0060458212") == "H&H Bagels Altamonte Springs"
    print("ok: DC code + ship-to account crosswalks")


def test_filename_parsing():
    assert account_from_filename(ALTAMONTE.name) == "60458212"
    assert snapshot_from_filename(ALTAMONTE.name) == "2026-08-03"
    assert account_from_filename("nope.csv") == ""
    print("ok: order-guide filename -> account + snapshot date")


def test_parses_real_altamonte_excerpt():
    rows, errors, meta = parse_order_guide(
        ALTAMONTE.read_text(), filename=ALTAMONTE.name)
    assert len(rows) == 30, len(rows)
    assert meta["account"] == "60458212"
    assert meta["store"] == "H&H Bagels Altamonte Springs"
    assert meta["dc_codes"] == ["3005"]
    assert meta["snapshot_date"] == "2026-08-03"
    assert all(r["warehouse"] == "Ocala, FL" for r in rows)
    assert all(r["case_cost"] is not None for r in rows)
    # All 12 H&H bagel varieties resolve from the Cheney item # crosswalk, and
    # every one is a 60 CT case at $30.00 -- confirmed against the 810s.
    hh = [r for r in rows if r["variety"]]
    assert {r["variety"] for r in hh} == _HH_VARIETIES, sorted({r["variety"] for r in hh})
    assert all(r["case_cost"] == 30.00 for r in hh)
    assert all(r["case_size"] == 1 and r["pack_size"] == "60CT" for r in hh)
    print("ok: real Altamonte order guide -> 30 priced rows, 12 H&H varieties")


def test_all_zero_on_hand_is_reported_not_applied():
    rows, errors, meta = parse_order_guide(
        ALTAMONTE.read_text(), filename=ALTAMONTE.name)
    # The whole point: Cheney's on-hand column is 0 on every row.
    assert meta["on_hand_populated"] is False
    assert meta["on_hand_nonzero_rows"] == 0
    assert any("NOT an" in e and "inventory snapshot" in e for e in errors), errors
    # ...and no event of any kind is produced from this file.
    assert not hasattr(parse_order_guide, "events")
    print("ok: all-zero on-hand surfaced as an error, no events emitted")


def test_out_of_scope_dc_is_flagged_not_invented():
    rows, errors, meta = parse_order_guide(
        CHAPEL_HILL.read_text(), filename=CHAPEL_HILL.name)
    assert meta["dc_codes"] == ["3012"]
    assert all(r["warehouse"] == "" and r["in_scope"] is False for r in rows)
    assert all(r["dc_name"] == "STATESVILLE" for r in rows)
    s = summarize(rows, meta)
    assert s["warehouses"] == []
    assert s["out_of_scope_dc"] == ["STATESVILLE"]
    # Chapel Hill's guide carries no H&H bagel SKUs at all.
    assert s["hh_varieties"] == []
    print("ok: Statesville rows flagged out-of-scope, no warehouse invented")


def test_on_hand_path_refuses_order_guide_files():
    for p in (ALTAMONTE, CHAPEL_HILL):
        events, errors = parse_inventory_csv(p.read_text(), filename=p.name)
        assert events == [], f"{p.name} produced on_hand events!"
        assert len(errors) == 1 and "OrderGuide" in errors[0], errors
        assert "cheney_order_guide" in errors[0]
    print("ok: on-hand parser refuses order-guide files by shape")


def test_all_zero_qty_column_refused_in_on_hand_csv():
    """A properly headed CSV whose on-hand column is all zeros is still refused
    -- that's the bug that would erase every warehouse's count."""
    header = "Item #,Description,DC,On Hand Cases,Case Size,Case Cost,Timestamp\n"
    zeros = header + "\n".join(
        f"1015301{i},BAGEL PLAIN,Ocala,0,60,30.00,2026-08-03" for i in range(8, 9))
    events, errors = parse_inventory_csv(zeros, filename="zeroed.csv")
    assert events == [], events
    assert any("zero or blank on all" in e for e in errors), errors
    # ...unless the caller explicitly asks for a zero-out.
    events2, _ = parse_inventory_csv(zeros, filename="zeroed.csv",
                                     allow_all_zero=True)
    assert len(events2) == 1 and events2[0]["item"]["quantity"] == 0.0
    # A real count with stock is unaffected.
    real = header + "10153018,BAGEL PLAIN,Ocala,7,60,30.00,2026-08-03"
    events3, _ = parse_inventory_csv(real, filename="real.csv")
    assert len(events3) == 1 and events3[0]["item"]["quantity"] == 7.0
    print("ok: all-zero on-hand column refused unless explicitly allowed")


def test_looks_like_order_guide_does_not_false_positive():
    assert looks_like_order_guide(ALTAMONTE.read_text())
    assert looks_like_order_guide(CHAPEL_HILL.read_text())
    assert not looks_like_order_guide(
        Path("integrations/examples/cheney_brothers_inventory.example.csv").read_text())
    assert not looks_like_order_guide("")
    assert not looks_like_order_guide("Item #,DC,On Hand\n100001,Ocala,4")
    print("ok: order-guide detection has no false positives on headed CSVs")


def test_empty_and_malformed():
    rows, errors, meta = parse_order_guide("", filename="x.csv")
    assert rows == [] and errors and meta["row_count"] == 0
    rows, errors, _ = parse_order_guide("a,b\nc,d\n", filename="x.csv")
    assert rows == [] and any("7-column" in e for e in errors), errors
    print("ok: empty + malformed order guides reported cleanly")


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
    print("\nall cheney_order_guide tests passed")
