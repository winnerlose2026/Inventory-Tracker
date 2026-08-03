"""Ordering rules for two copies of the same PO.

Anchored on the real Houston incident (PO 305202B2) and on JD's description of
US Foods' behaviour: they amend a PO without changing the PO number, labelling
the re-send "REVISED" or "REPRINT".
"""
from integrations.po_revision import is_newer, rev_int, is_numeric_rev, parse_dt

GREENE = "2026-07-30T15:57:55Z"   # 305202B2 REPRINT   -> Plain 176 (stale)
FOLEY  = "2026-07-30T19:05:38Z"   # 305202B2 0000003   -> Plain 168 (correct)


# --- the incident -----------------------------------------------------------

def test_numbered_revision_beats_an_earlier_reprint():
    """The actual bug: REPRINT arrived FIRST, so it must lose."""
    assert is_newer("0000003", FOLEY, "REPRINT", GREENE) is True


def test_earlier_reprint_does_not_beat_a_later_numbered_revision():
    assert is_newer("REPRINT", GREENE, "0000003", FOLEY) is False


# --- JD: USF amends without bumping the number ------------------------------

def test_later_reprint_supersedes_an_earlier_numbered_revision():
    """USF re-sends an amended PO as REPRINT rather than rev 4."""
    assert is_newer("REPRINT", "2026-08-02T10:00:00Z",
                    "0000003", FOLEY) is True


def test_later_revised_supersedes_an_earlier_numbered_revision():
    assert is_newer("REVISED", "2026-08-02T10:00:00Z",
                    "0000003", FOLEY) is True


def test_later_reprint_supersedes_an_earlier_reprint():
    assert is_newer("REPRINT", "2026-08-02T10:00:00Z",
                    "REPRINT", GREENE) is True


# --- guard: a stale forward must not clobber a newer revision ---------------

def test_stale_forward_of_an_older_number_cannot_win_on_date():
    """JD forwards an old copy; it lands with a brand-new received time.
    Both tokens are numeric, so the number wins and the forward is ignored."""
    assert is_newer("0000001", "2026-08-03T09:00:00Z",
                    "0000003", FOLEY) is False


def test_higher_number_wins_even_when_it_arrives_earlier():
    assert is_newer("0000005", "2026-07-01T00:00:00Z",
                    "0000002", "2026-07-20T00:00:00Z") is True


# --- idempotency ------------------------------------------------------------

def test_identical_document_is_not_newer():
    assert is_newer("0000003", FOLEY, "0000003", FOLEY) is False


def test_same_number_same_instant_is_a_duplicate():
    assert is_newer("REPRINT", GREENE, "REPRINT", GREENE) is False


def test_unrevised_cheney_style_po_replays_idempotently():
    """Cheney POs expose no revision at all -> rev 0, equal dates."""
    assert is_newer("", GREENE, "", GREENE) is False


# --- legacy rows with no stored date ---------------------------------------

def test_dated_incoming_supersedes_a_row_stored_before_the_date_field():
    """Legacy rows have no source_received_at, so they are necessarily older.
    A dated non-numeric re-issue supersedes them. Two rows with the SAME
    number are a duplicate, though -- idempotency must hold."""
    assert is_newer("REPRINT", FOLEY, "REPRINT", "") is True
    assert is_newer("REPRINT", FOLEY, "0000004", "") is True
    assert is_newer("0000003", FOLEY, "0000003", "") is False


def test_undated_non_numeric_incoming_never_beats_a_dated_stored_row():
    """An undated REPRINT can't supersede a dated revision -- there's no
    evidence it came later. (A higher NUMBER still wins undated: that's
    rule 1, asserted by test_higher_number_wins_even_when_it_arrives_earlier.)"""
    assert is_newer("REPRINT", "", "0000003", FOLEY) is False
    assert is_newer("", "", "0000003", FOLEY) is False


def test_no_dates_anywhere_falls_back_to_numeric():
    assert is_newer("0000004", "", "0000003", "") is True
    assert is_newer("0000003", "", "0000004", "") is False


# --- helpers ----------------------------------------------------------------

def test_rev_int_does_not_promote_non_numeric_to_a_sentinel():
    """The old _po_rev_int returned 10_000_000 here. That was the bug."""
    assert rev_int("REPRINT") == 0
    assert rev_int("REVISED") == 0
    assert rev_int("0000012") == 12
    assert rev_int("") == 0
    assert rev_int(None) == 0


def test_is_numeric_rev():
    assert is_numeric_rev("0000003") is True
    assert is_numeric_rev("REPRINT") is False
    assert is_numeric_rev("") is False


def test_parse_dt_handles_z_offset_and_bare_date():
    assert parse_dt("2026-07-30T19:05:38Z") is not None
    assert parse_dt("2026-07-30T19:05:38+00:00") is not None
    assert parse_dt("2026-07-30") is not None
    assert parse_dt("") is None
    assert parse_dt("garbage") is None
    # Z and explicit offset must compare equal.
    assert parse_dt("2026-07-30T19:05:38Z") == parse_dt("2026-07-30T19:05:38+00:00")


# --- end-to-end: the real Houston incident through the apply path -----------

def test_houston_reprint_then_revision_books_1120_not_1128(tmp_path):
    """PO 305202B2. Greene's REPRINT (Plain 176) arrived at 15:57; Foley's
    rev 0000003 (Plain 168) at 19:05. Ordering by received time must leave
    Plain at 168, i.e. the PO totals its true 1120 cs."""
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    import test_on_order_dedup as T
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent

    it, sync = T._setup_temp_inventory(tmp_path)
    T._seed(it)

    def evt(qty, rev, received):
        return EmailEvent(
            event_type="restock",
            item=SyncItem(quantity=qty, distributor="US Foods", variety="Plain",
                          warehouse="La Mirada, CA", unit="cases"),
            source_message_id="m", source_subject="US Foods Houston PO: 305202B2",
            po_number="305202B2", po_revision=rev, source_received_at=received,
        )

    key = "plain bagel 4oz [usf - la mirada]"

    # Greene's REPRINT lands first with the stale 176.
    sync._apply_events([evt(176.0, "REPRINT", GREENE)], dry_run=False)
    inv = it._load(it.INVENTORY_FILE)
    assert [p["qty"] for p in inv[key]["on_order"]] == [176.0]

    # Foley's numbered revision lands later and must win.
    sync._apply_events([evt(168.0, "0000003", FOLEY)], dry_run=False)
    inv = it._load(it.INVENTORY_FILE)
    pending = inv[key]["on_order"]
    assert len(pending) == 1, f"expected 1 pending, got {len(pending)}"
    assert pending[0]["qty"] == 168.0
    assert pending[0]["po_revision"] == "0000003"


def test_replaying_the_stale_reprint_afterwards_is_ignored(tmp_path):
    """A later re-scan re-reads Greene's older email; it must not win again.

    A pending-only PO has no usage row yet, so this exercises
    sync_inventory._newest_pending_doc -- without it the stale copy was
    appended as a SECOND pending row instead of being skipped.
    """
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    import test_on_order_dedup as T
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent

    it, sync = T._setup_temp_inventory(tmp_path)
    T._seed(it)

    def evt(qty, rev, received):
        return EmailEvent(
            event_type="restock",
            item=SyncItem(quantity=qty, distributor="US Foods", variety="Plain",
                          warehouse="La Mirada, CA", unit="cases"),
            source_message_id="m", source_subject="US Foods Houston PO: 305202B2",
            po_number="305202B2", po_revision=rev, source_received_at=received,
        )

    key = "plain bagel 4oz [usf - la mirada]"
    sync._apply_events([evt(168.0, "0000003", FOLEY)], dry_run=False)
    sync._apply_events([evt(176.0, "REPRINT", GREENE)], dry_run=False)
    inv = it._load(it.INVENTORY_FILE)
    pending = inv[key]["on_order"]
    assert len(pending) == 1
    assert pending[0]["qty"] == 168.0, "stale REPRINT clobbered the correction"
