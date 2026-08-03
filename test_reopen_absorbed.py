"""Reopening an Arrived PO must not subtract cases a count already excludes.

``api_pending_reopen`` pulls a rollover's cases back out of on-hand on the
assumption that they are still sitting there. That only holds while no count has
superseded the rollover. A reported on-hand figure is ABSOLUTE: once a count
dated at-or-after the arrival is synced, on-hand *is* that count and the delivery
is baked into it, so subtracting removes the cases a second time -- clamped at
zero, which quietly destroys the number rather than going negative.

2026-08-03, the case that surfaced it: Alcoa sat at 93 cs on a 17:46 count while
PO 2055126H's 112 cs were still in transit (real arrival 8/4, not the 8/3 the
ship+7 rule derived). Reopening the PO to put those 112 cs back on order would
have driven all 12 Alcoa SKUs to ~0.

The invariant tied together here: reopen subtracts EXACTLY when
``sync_inventory._receipts_after_count`` would have re-added. The two are
mirrors, so they must agree on every input -- if they ever disagree, cases are
either double-counted or destroyed.
"""
from __future__ import annotations

import blueprints.pos as pos
import sync_inventory as sync

KEY = "plain bagel 4oz [usf - alcoa]"


def _roll(qty, arrival, *, ts="2026-08-03T13:34:38", note=""):
    return {"item_key": KEY, "amount": -qty, "source": "on_order_rollover",
            "arrival_date": arrival, "timestamp": ts, "note": note,
            "po_number": "2055126H"}


def _item(qty, count_at):
    return {"quantity": qty, "last_count_at": count_at, "name": "Plain Bagel 4oz"}


# --- the predicate --------------------------------------------------------

def test_arrival_after_the_count_is_still_a_separate_addend():
    """Count is older than the delivery -> _receipts_after_count stacked the
    cases on top, so reopen must take them back off."""
    row = _roll(8, "2026-08-03T00:00:00")
    assert pos._rollover_still_in_onhand(row, _item(28, "2026-07-27T19:03:46")) is True


def test_arrival_before_the_count_is_absorbed():
    """Count postdates the delivery -> the counted figure already includes it."""
    row = _roll(8, "2026-07-20T00:00:00")
    assert pos._rollover_still_in_onhand(row, _item(28, "2026-07-27T19:03:46")) is False


def test_same_day_counts_as_absorbed():
    """Day granularity is all a report gives us; matches the conservative
    same-day rule in _receipts_after_count."""
    row = _roll(8, "2026-07-27T13:34:38")
    assert pos._rollover_still_in_onhand(row, _item(28, "2026-07-27T19:03:46")) is False


def test_never_counted_means_the_rollover_is_all_there_is():
    row = _roll(8, "2026-08-03T00:00:00")
    assert pos._rollover_still_in_onhand(row, _item(8, "")) is True
    assert pos._rollover_still_in_onhand(row, {"quantity": 8}) is True


def test_arrival_falls_back_to_the_note_eta_on_legacy_rows():
    """Rows written before arrival_date existed carry '(ETA YYYY-MM-DD)'."""
    row = _roll(8, "", note="Rolled over from on_order (ETA 2026-08-03)")
    assert pos._rollover_still_in_onhand(row, _item(28, "2026-07-27T19:03:46")) is True
    assert pos._rollover_still_in_onhand(row, _item(28, "2026-08-04T09:00:00")) is False


# --- the live 2026-08-03 Alcoa scenario ----------------------------------

def test_alcoa_20260803_reopen_preserves_the_93_case_count():
    """The exact production state: 8/3 17:46 count, 112 cs 'arrived' 8/3 13:34
    that had not actually landed. On-hand must survive the reopen untouched."""
    row = _roll(9.33, "2026-08-03T13:34:38.720820")
    item = _item(93.0, "2026-08-03T17:46:30Z")
    assert pos._rollover_still_in_onhand(row, item) is False
    # ...and the count sync agrees it was never stacked on top:
    assert sync._receipts_after_count([row], KEY, "2026-08-03T17:46:30Z") == 0


def test_alcoa_pre_count_state_would_still_be_unrolled():
    """Before the 17:46 count landed, on-hand DID carry the rollover (the 7/27
    count predates the arrival), so reopen correctly subtracts."""
    row = _roll(9.33, "2026-08-03T13:34:38.720820")
    item = _item(102.33, "2026-07-27T19:03:46+00:00")
    assert pos._rollover_still_in_onhand(row, item) is True
    assert sync._receipts_after_count([row], KEY, "2026-07-27T19:03:46+00:00") == 9.33


# --- the mirror invariant ------------------------------------------------

def test_reopen_and_count_sync_never_disagree():
    """Reopen subtracts iff the count sync would have re-added. Disagreement in
    either direction means cases are double-counted or destroyed."""
    arrivals = ["2026-07-20T00:00:00", "2026-07-27T00:00:00",
                "2026-08-03T00:00:00", "2026-08-03T13:34:38.720820",
                "2026-08-10T00:00:00"]
    counts = ["2026-07-27T19:03:46+00:00", "2026-08-03T17:46:30Z",
              "2026-08-05T08:00:00", "2026-08-10T12:00:00"]
    for arrival in arrivals:
        for count_at in counts:
            row = _roll(8, arrival)
            subtracts = pos._rollover_still_in_onhand(row, _item(50, count_at))
            re_added = sync._receipts_after_count([row], KEY, count_at) > 0
            assert subtracts == re_added, (arrival, count_at, subtracts, re_added)


def test_reversed_rows_are_ignored_by_the_count_sync():
    """Reopen marks rows reversed; a later count must not re-add them."""
    row = _roll(8, "2026-08-03T00:00:00")
    row["reversed"] = True
    assert sync._receipts_after_count([row], KEY, "2026-07-27") == 0
