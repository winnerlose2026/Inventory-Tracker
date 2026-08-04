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


def test_same_layout_with_on_hand_populated_becomes_a_real_snapshot():
    """The most likely shape of the fix Cheney owes us: same headerless
    7-column export, on-hand column filled in. That MUST flow through to
    on_hand events rather than being dismissed as a price list."""
    csv_text = (
        "10153018,H & H,05,14,001 60CT,30.00,20260803113325\n"
        "10153048,H & H,05,9,001 60CT,30.00,20260803113325\n"
        "10153046,H & H,05,0,001 60CT,30.00,20260803113325\n"
    )
    fn = "OrderGuide-20260803-113325_0060458212.csv"
    rows, errors, meta = parse_order_guide(csv_text, filename=fn)
    assert meta["on_hand_populated"] is True
    assert meta["on_hand_nonzero_rows"] == 2
    # No "this is a price list" complaint when there IS on-hand data.
    assert not any("NOT an" in e for e in errors), errors

    events, errs = parse_inventory_csv(csv_text, filename=fn)
    assert len(events) == 3, (len(events), errs)
    by_variety = {e["item"]["variety"]: e for e in events}
    assert set(by_variety) == {"Plain", "Everything", "Cinnamon Raisin"}
    plain = by_variety["Plain"]["item"]
    assert plain["quantity"] == 14.0
    assert plain["warehouse"] == "Ocala, FL"
    assert plain["unit"] == "cs"
    assert plain["case_size"] == 60          # "001 60CT" -> 60 per case, not 1
    assert plain["case_cost"] == 30.00
    assert plain["distributor_sku"] == "10153018"
    assert by_variety["Plain"]["count_date"] == "2026-08-03"
    # A genuine zero row still rides along -- it's a real count of zero here.
    assert by_variety["Cinnamon Raisin"]["item"]["quantity"] == 0.0
    print("ok: order-guide layout WITH on-hand converts to on_hand events")


def test_units_per_case_from_pack_string():
    """Count packs multiply; weight/volume packs don't."""
    csv_text = (
        "10153018,H & H,05,3,001 60CT,30.00,20260803113325\n"
        "164011,KRAFT,05,2,004 5LB,2.80,20260803113325\n"
        "102309,COKE,05,5,024 12OZ,49.27,20260803113325\n"
    )
    rows, _errors, meta = parse_order_guide(csv_text, filename="OrderGuide-20260803-113325_0060458212.csv")
    assert meta["on_hand_populated"] is True
    packs = {r["item_no"]: (r["case_size"], r["pack_size"]) for r in rows}
    assert packs["10153018"] == (1, "60CT")
    assert packs["164011"] == (4, "5LB")
    assert packs["102309"] == (24, "12OZ")
    # Only the H&H item resolves to a variety, so only it becomes an event.
    events, _ = parse_inventory_csv(csv_text, filename="OrderGuide-20260803-113325_0060458212.csv")
    assert len(events) == 1
    assert events[0]["item"]["case_size"] == 60      # 1 x 60 CT
    print("ok: 60CT pack -> case_size 60; weight/volume packs use pack count")


def test_untracked_dc_rows_are_skipped_with_a_reason():
    """Statesville has on-hand but no tracker warehouse -- skip, don't guess,
    and don't claim the file had no on-hand data when it did."""
    csv_text = "10153018,H & H,12,11,001 60CT,30.00,20260803113325\n"
    fn = "OrderGuide-20260803-113325_0060415887.csv"
    events, errors = parse_inventory_csv(csv_text, filename=fn)
    assert events == []
    joined = " ".join(errors)
    assert "does carry on-hand quantities" in joined, errors
    assert "nothing to apply" in joined, errors
    assert "no on-hand quantities at all" not in joined, errors
    assert any("DC the tracker doesn't model" in e for e in errors), errors
    print("ok: untracked-DC rows skipped with an accurate reason")


def test_nonzero_untracked_rows_do_not_unlock_a_bagel_zero_out():
    """The guard must measure the rows we would WRITE, not the whole catalog.
    Cheney distributes ~220 third-party items alongside the ~12 H&H SKUs; a
    case of Coke being in stock says nothing about the bagel rows."""
    csv_text = (
        "10064422,SCHREIBE,05,7,004 5LB,4.17,20260803113325\n"    # nonzero, untracked
        "164011,KRAFT,05,3,004 5LB,2.80,20260803113325\n"         # nonzero, untracked
        "10153018,H & H,05,0,001 60CT,30.00,20260803113325\n"     # tracked, ZERO
        "10153048,H & H,05,0,001 60CT,30.00,20260803113325\n"
        "10153019,H & H,05,0,001 60CT,30.00,20260803113325\n"
    )
    fn = "OrderGuide-20260803-113325_0060458212.csv"
    rows, _errs, meta = parse_order_guide(csv_text, filename=fn)
    # File-level view says "populated" -- and that view is NOT what gates us.
    assert meta["on_hand_populated"] is True
    events, errors = parse_inventory_csv(csv_text, filename=fn)
    assert events == [], [e["item"] for e in events]
    assert any("0 for all 3 tracked item(s)" in e for e in errors), errors
    # One nonzero bagel row is enough to make it a real snapshot again.
    fixed = csv_text.replace(
        "10153018,H & H,05,0,", "10153018,H & H,05,6,")
    events2, _ = parse_inventory_csv(fixed, filename=fn)
    assert len(events2) == 3
    assert {e["item"]["variety"]: e["item"]["quantity"] for e in events2} == {
        "Plain": 6.0, "Everything": 0.0, "Poppy Seed": 0.0}
    print("ok: nonzero untracked rows can't unlock a zero-out of tracked SKUs")


def test_unmapped_catalog_rows_are_summarized_not_listed():
    """A real order guide is ~236 rows, ~224 of them third-party. One error per
    row would bury the health surface in ~1,800 lines per daily drop."""
    rows = ["10153018,H & H,05,4,001 60CT,30.00,20260803113325"]
    rows += [f"9{i:06d},BRAND{i},05,2,024 12OZ,10.00,20260803113325"
             for i in range(40)]
    fn = "OrderGuide-20260803-113325_0060458212.csv"
    events, errors = parse_inventory_csv("\n".join(rows) + "\n", filename=fn)
    assert len(events) == 1 and events[0]["item"]["variety"] == "Plain"
    unmapped = [e for e in errors if "not H&H items we track" in e]
    assert len(unmapped) == 1, errors
    assert "40 catalog row(s)" in unmapped[0], unmapped
    assert "+35 more" in unmapped[0], unmapped
    assert len(errors) <= 2, errors
    print("ok: unmapped catalog rows reported once, not one error per row")


def test_split_units_column_does_not_steal_the_qty_role():
    """A feed reporting splits AND cases on hand must bind qty to the CASES
    column. Binding to 'Split Units On Hand' both misreads splits as cases and
    trips the all-zero guard on a good snapshot."""
    header = ("Item #,Description,DC,Split Units On Hand,Cases On Hand,"
              "Case Size,Case Cost,Snapshot Timestamp\n")
    body = ("10153018,BAGEL PLAIN PARBAKED,Ocala,0,14,60,30.00,2026-08-03\n"
            "10153048,BAGEL EVERYTHING PARBAKED,Ocala,0,9,60,30.00,2026-08-03\n")
    events, errors = parse_inventory_csv(header + body, filename="split.csv")
    assert len(events) == 2, (len(events), errors)
    qty = {e["item"]["variety"]: e["item"]["quantity"] for e in events}
    assert qty == {"Plain": 14.0, "Everything": 9.0}, qty
    print("ok: 'Cases On Hand' wins the qty role over 'Split Units On Hand'")


def test_normalize_dc_code_refuses_ambiguous_input():
    # Zero-padded short codes still resolve.
    assert normalize_dc_code("005") == "3005"
    assert normalize_dc_code("012") == "3012"
    assert normalize_dc_code("003005") == "3005"
    # Always 4 digits or empty -- never a 5-character code.
    for bad in ("", "0", "00", "300", "30012", "abc", "-"):
        out = normalize_dc_code(bad)
        assert out == "" or len(out) == 4, (bad, out)
    assert normalize_dc_code("300") == ""     # ambiguous, refused not guessed
    assert warehouse_from_dc_code("300") == ""
    print("ok: normalize_dc_code returns 4 digits or nothing")


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
