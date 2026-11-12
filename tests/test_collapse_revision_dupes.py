"""_collapse_revision_dupes orders two copies of a PO line the way the apply
path does (po_revision.is_newer), and only rewrites the SKUs it changed."""
import sys

sys.path.insert(0, ".")

import inventory_tracker as it


def _e(rev, qty, received="", sender="tom.foley@usfoods.com", ordered="2026-08-25T00:00:00"):
    return {"po_number": "505876B2", "po_revision": rev, "qty": qty,
            "ordered_at": ordered, "source_received_at": received,
            "source_sender": sender}


def test_later_document_wins_even_with_a_lower_revision_number():
    inv = {"a": {"on_order": [_e("0000006", 32, "2026-08-25T17:03:00Z"),
                              _e("0000003", 40, "2026-08-25T18:20:00Z")]}}
    assert it._collapse_revision_dupes(inv) is True
    assert [(e["po_revision"], e["qty"]) for e in inv["a"]["on_order"]] == [("0000003", 40)]


def test_a_reprint_is_ordered_by_date_not_treated_as_newest():
    inv = {"a": {"on_order": [_e("REPRINT", 176, "2026-07-30T15:57:00Z"),
                              _e("0000003", 168, "2026-07-30T19:05:00Z")]}}
    it._collapse_revision_dupes(inv)
    assert [e["qty"] for e in inv["a"]["on_order"]] == [168]


def test_internal_forward_never_beats_the_vendor_copy():
    inv = {"a": {"on_order": [_e("0000003", 168, "2026-07-30T19:05:00Z"),
                              _e("REPRINT", 176, "2026-07-31T09:00:00Z",
                                 sender="jd@hhbagels.com")]}}
    it._collapse_revision_dupes(inv)
    assert [e["qty"] for e in inv["a"]["on_order"]] == [168]


def test_undated_legacy_rows_fall_back_to_the_revision_number():
    inv = {"a": {"on_order": [_e("0000001", 8), _e("0000002", 16)]}}
    it._collapse_revision_dupes(inv)
    assert [e["po_revision"] for e in inv["a"]["on_order"]] == ["0000002"]


def test_same_revision_split_lines_are_kept_and_untouched_items_are_not_rewritten():
    split = [_e("0000002", 48, "2026-08-25T18:20:00Z", ordered="2026-09-01T00:00:00"),
             _e("0000002", 32, "2026-08-25T18:20:00Z", ordered="2026-08-01T00:00:00")]
    other_po = dict(_e("0000001", 5, ordered="2026-01-01T00:00:00"), po_number="OTHER")
    inv = {"changed": {"on_order": [_e("0000001", 8, "2026-08-01T00:00:00Z"),
                                    _e("0000002", 16, "2026-08-02T00:00:00Z")]},
           "split": {"on_order": list(split)},
           "mixed": {"on_order": [split[0], other_po]}}
    split_list, mixed_list = inv["split"]["on_order"], inv["mixed"]["on_order"]
    assert it._collapse_revision_dupes(inv) is True
    assert len(inv["changed"]["on_order"]) == 1
    # not re-sorted, not replaced: the very same list objects, same order
    assert inv["split"]["on_order"] is split_list and split_list == split
    assert inv["mixed"]["on_order"] is mixed_list
    assert it._collapse_revision_dupes(inv) is False


def test_two_undated_rows_keep_the_legacy_reprint_rule():
    inv = {"a": {"on_order": [_e("0000004", 8), _e("REPRINT", 88)]}}
    it._collapse_revision_dupes(inv)
    assert [e["po_revision"] for e in inv["a"]["on_order"]] == ["REPRINT"]
