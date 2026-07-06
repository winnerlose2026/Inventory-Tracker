"""Tests for the Cheney daily on-hand CSV parser (SFTP feed).
Standalone or pytest. Uses descriptions that resolve via the shared variety
map so it doesn't hard-depend on specific Cheney item numbers."""
import sys
sys.path.insert(0, ".")
from integrations.cheney_csv_inventory import parse_inventory_csv

CSV = (
    "Cheney Item #,Product Description,DC,On Hand Cases,Case Size,Case Cost,Snapshot Timestamp\n"
    "100001,Plain Bagel,Riviera Beach,44,60,26.50,2026-07-06\n"
    "100002,Everything Bagel,Ocala,8,60,26.50,07/06/2026\n"
    "100003,Sesame Bagel,Punta Gorda,33,60,,2026-07-06\n"
)


def test_parses_on_hand_events():
    events, errors = parse_inventory_csv(CSV)
    assert not errors, errors
    assert len(events) == 3, [e["item"]["variety"] for e in events]
    by = {(e["item"]["variety"], e["item"]["warehouse"]): e for e in events}
    e = by[("Plain", "Riviera Beach, FL")]
    assert e["event_type"] == "on_hand"
    assert e["item"]["quantity"] == 44
    assert e["item"]["unit"] == "cs"
    assert e["item"]["case_size"] == 60
    assert e["item"]["case_cost"] == 26.50
    assert e["item"]["distributor"] == "Cheney Brothers"
    assert e["count_date"] == "2026-07-06"
    # M/D/Y timestamp also normalizes
    assert by[("Everything", "Ocala, FL")]["count_date"] == "2026-07-06"
    # blank case cost -> CHENEY_CASE_COST default
    assert by[("Sesame", "Punta Gorda, FL")]["item"]["case_cost"] == 26.50
    print("ok: on_hand events with variety/warehouse/count_date/case cost")


def test_unknown_dc_and_unmapped_variety_flagged():
    csv2 = (
        "Item,Description,Facility,Cases\n"
        "100001,Plain Bagel,Atlanta,10\n"       # unknown DC
        "999999,Glazed Donut,Ocala,5\n"          # unmapped variety
    )
    events, errors = parse_inventory_csv(csv2)
    assert events == [], events
    assert any("unknown DC" in e for e in errors), errors
    assert any("unmapped" in e for e in errors), errors
    print("ok: unknown DC + unmapped variety flagged, no bad events")


def test_missing_required_columns():
    events, errors = parse_inventory_csv("foo,bar\n1,2\n")
    assert events == [] and errors and "required columns" in errors[0]
    print("ok: missing required columns reported")


def test_empty():
    assert parse_inventory_csv("")[0] == []
    print("ok: empty file")


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
    print("\nall cheney_csv_inventory tests passed")
