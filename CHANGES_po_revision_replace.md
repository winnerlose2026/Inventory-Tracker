# PO revision-replace semantics — changes

Closes the open follow-up from `CHANGES_usf_po_pdf.md`: a later revision
of the same USF PO now fully supersedes earlier restock events instead
of stacking on top of them.

## Why

USF emits revised POs in place — same PO number, incremented revision
(`3885495O` rev `0000001` -> rev `0000002`). Without revision handling,
each re-scan or revision double-counted restock quantities in our
usage log and inflated on-hand in `data/inventory.json`.

## Design

- **Key = PO number.** All events from a parsed PO share the same
  `po_number` / `po_revision`, so grouping by PO number is safe.
- **Revision = integer.** Parsed as `int(str.lstrip("0"))` so
  `"0000002"`, `"2"`, and `"00000002"` all compare as `2`.
- **Whole-PO replace, not line-by-line diff.** Reversing everything
  from the old rev and re-posting the new rev is simpler, easier to
  audit, and correctly handles added/dropped lines.
- **Non-destructive.** Prior usage rows aren't deleted — they're
  marked `superseded_by_revision=<new_rev>` and a mirror reversal row
  is appended so the log still reconciles.
- **Idempotent on replay.** If `new_rev <= existing_rev`, the group is
  skipped. Re-scanning the same mailbox is safe.
- **Warn, don't fail, on negative on-hand.** If stock was consumed
  between revisions, reversing the old rev may drive on-hand below
  zero. We log a `warning` in the scan report and continue — someone
  still needs to reconcile, but the new rev lands.

## Files

### Modified: `integrations/email_scanner.py`

`EmailEvent` gains two fields, populated only for events sourced from
a distributor PO:

    @dataclass
    class EmailEvent:
        ...
        po_number: str = ""
        po_revision: str = ""

`_usfoods_po_to_events` now sets `po_number` / `po_revision` on each
event from `UsFoodsPO.po_number` / `UsFoodsPO.po_revision`. Non-PO
events (CSV attachments, body-tag fallback) leave both fields empty.

### Modified: `sync_inventory.py`

Three new helpers above `_apply_email_event`:

- **`_po_rev_int(s)`** — coerces a revision string to an int.
  `"0000002"` -> `2`, `""` or invalid -> `0`.

- **`_highest_applied_rev(usage, po_number)`** — scans the usage log
  for active entries (tagged with this PO, not marked superseded, not
  themselves reversal audit rows) and returns the highest rev seen
  plus their indices.

- **`_reverse_po_entries(po_number, new_rev, active_indices, inv,
  usage, now, report, dry_run)`** — rolls inventory back by the
  restock amount on each active entry, marks the entry
  `superseded_by_revision`, appends a mirror audit row with
  `reversal_of_revision=<old_rev>`, and emits a `warnings` entry if
  on-hand would drop below zero.

`_apply_email_event` now tags the appended usage row with
`po_number` / `po_revision` when the event has them, so
`_highest_applied_rev` can find it later.

`scan_email` restructures its application loop:

    1. Count by_event_type across all events (unchanged behavior)
    2. Split events into PO groups (by po_number) and non-PO events
    3. For each PO group:
         a. new_rev_int = _po_rev_int(group[0].po_revision)
         b. existing_rev_int, active_idx = _highest_applied_rev(usage, po)
         c. if existing_rev_int and new_rev_int <= existing_rev_int:
                skip — record in report["po_revisions_skipped"]
         d. if existing_rev_int (and new is higher):
                _reverse_po_entries(...)
                record in report["po_revisions_superseded"]
         e. for each event in the group: _apply_email_event(...)
    4. Non-PO events apply as before.

Two new report keys:
- `po_revisions_skipped: list[str]` — human-readable skip notices
- `po_revisions_superseded: list[str]` — notices for reversed revs
- `warnings: list[str]` — negative-on-hand situations to reconcile

## Verification

`test_po_revision.py` (in `outputs/`) walks five scenarios against a
stubbed `integrations` / `inventory_tracker` pair:

    STEP 1  rev 1 applied         [OK]  3 restocks, on-hand matches, po-tagging correct
    STEP 2  rev 2 applied         [OK]  rev 1 reversed + superseded, rev 2 booked clean
    STEP 3  rev 2 replayed        [OK]  idempotent no-op, usage log unchanged
    STEP 4  stale rev 1 replay    [OK]  skipped — does not reopen rev 2
    STEP 5  consumption mid-rev   [OK]  warning fires; on-hand = 3 - 10 + 14 = 7

Scenario 2 specifically covers:
- line changed qty          (Plain: 8 -> 12)
- line dropped entirely     (Sesame: 6 -> 0)
- line unchanged            (Everything: 10 -> 10)
- line added                (Poppy Seed: 0 -> 4)

Final state matches rev 2 applied fresh. Usage log contains:
3 superseded rev 1 rows + 3 reversal audit rows + 3 fresh rev 2 rows = 9.

## Follow-ups (not in this change)

- **`reconcile` CLI view.** `scan_email` now writes
  `po_revisions_superseded` / `warnings` into the report dict, but
  the pretty-printer (`_print_report`) doesn't surface them yet.
  Easy to add — the info is already in the report.
- **Dropped-line handling policy.** Current behavior: a line
  dropped in rev 2 effectively zeroes that SKU for the PO (rev 1's
  contribution is reversed, nothing new posts). If USF intends
  "drop = cancel", that's correct. If USF sometimes re-sends a
  reduced rev meaning "this is the shipped subset", the right
  behavior is the same, just with smaller quantities in rev 2.
- **Cross-PO cancellations.** If USF ever issues a second PO that
  cancels an earlier PO (different po_number), this logic doesn't
  catch it. Needs subject-line / body parsing to link POs.
