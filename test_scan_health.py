#!/usr/bin/env python3
"""Regression tests for the scan-health / count-date / body-only logic
(roadmap #4). Runs under pytest OR standalone (`python3 test_scan_health.py`).
All offline -- no network, no Graph, no disk inventory required.
"""
import sys
from datetime import datetime, timedelta, timezone
import email
from email.message import EmailMessage

sys.path.insert(0, ".")

from integrations.email_scanner import (
    _msg_event_candidate, _msg_date_iso, parse_message_with_errors, EmailEvent,
    _carries_report_payload, _is_auto_reply, _is_report_shaped,
)
from integrations.email_scanner import SyncItem  # re-exported
from sync_inventory import _apply_email_event
from inventory_tracker import warehouse_freshness, STALE_COUNT_DAYS


def test_msg_event_candidate_matches_distributor_and_rep_not_noise():
    zeb = {"from": {"emailAddress": {"address": "maria.hernandez@usfoods.com"}},
           "toRecipients": [{"emailAddress": {"address": "JD@hhbagels.com"}}]}
    reply_to_dist = {"from": {"emailAddress": {"address": "info@hhbagels.com"}},
                     "toRecipients": [{"emailAddress": {"address": "rep@cheneybrothers.com"}}]}
    noise = {"from": {"emailAddress": {"address": "news@ordoro.com"}},
             "toRecipients": [{"emailAddress": {"address": "info@hhbagels.com"}}]}
    assert _msg_event_candidate(zeb) is True
    assert _msg_event_candidate(reply_to_dist) is True
    assert _msg_event_candidate(noise) is False


def test_msg_date_iso_parses_and_handles_missing():
    m = EmailMessage(); m["Date"] = "Mon, 15 Jun 2026 14:38:19 +0000"
    assert _msg_date_iso(m).startswith("2026-06-15T14:38:19")
    assert _msg_date_iso(EmailMessage()) == ""


def test_on_hand_uses_count_date_not_ingest_time():
    evt = EmailEvent(
        event_type="on_hand",
        item=SyncItem(quantity=42.0, distributor="US Foods", variety="Plain",
                      warehouse="Zebulon, NC", name="Plain Bagel 4oz [USF - Zebulon]",
                      unit="cs", case_size=60, price=0.0, case_cost=27.0,
                      weekly_usage=None),
        count_date="2026-06-15T14:38:19+00:00",
    )
    key = "plain bagel 4oz [usf - zebulon]"
    inv = {key: {"name": "Plain Bagel 4oz [USF - Zebulon]", "quantity": 999.0,
                 "warehouse": "Zebulon, NC", "distributor": "US Foods",
                 "variety": "Plain", "unit": "cs", "case_size": 60, "price": 0.0,
                 "case_cost": 27.0, "weekly_usage": 0.0, "on_order": [],
                 "updated": "", "last_synced": "", "last_count_at": ""}}
    report = {"unmatched": [], "changes": [], "updated": 0, "unchanged": 0}
    _apply_email_event(evt, inv, [], "2026-06-22T23:59:59", report, dry_run=False)
    it = inv[key]
    assert report["unmatched"] == [], "event should have matched"
    assert it["last_count_at"] == "2026-06-15T14:38:19+00:00", it["last_count_at"]
    assert it["last_synced"] == "2026-06-22T23:59:59", "sync time stays ingest time"


def test_on_hand_does_not_regress_older_count():
    """A re-sent / out-of-order OLDER report must not overwrite a newer count
    (nor its quantities). Guards against the cross-scan regression where a
    re-scanned 6/22 sheet clobbered the already-applied 6/29 count."""
    evt = EmailEvent(
        event_type="on_hand",
        item=SyncItem(quantity=42.0, distributor="US Foods", variety="Plain",
                      warehouse="Zebulon, NC", name="Plain Bagel 4oz [USF - Zebulon]",
                      unit="cs", case_size=60, price=0.0, case_cost=27.0,
                      weekly_usage=None),
        count_date="2026-06-15T14:38:19+00:00",   # OLDER than the item's count
    )
    key = "plain bagel 4oz [usf - zebulon]"
    inv = {key: {"name": "Plain Bagel 4oz [USF - Zebulon]", "quantity": 999.0,
                 "warehouse": "Zebulon, NC", "distributor": "US Foods",
                 "variety": "Plain", "unit": "cs", "case_size": 60, "price": 0.0,
                 "case_cost": 27.0, "weekly_usage": 0.0, "on_order": [],
                 "updated": "", "last_synced": "", "last_count_at": "2026-06-22T16:00:00"}}
    report = {"unmatched": [], "changes": [], "updated": 0, "unchanged": 0}
    _apply_email_event(evt, inv, [], "2026-06-22T23:59:59", report, dry_run=False)
    it = inv[key]
    assert it["last_count_at"] == "2026-06-22T16:00:00", it["last_count_at"]
    assert it["quantity"] == 999.0, "older report must not overwrite newer qty"
    assert report["unchanged"] == 1, report


def test_newest_report_wins_ordering():
    older = EmailEvent(event_type="on_hand", item=SyncItem(quantity=1, distributor="x"),
                       count_date="2026-06-08T10:00:00+00:00")
    newer = EmailEvent(event_type="on_hand", item=SyncItem(quantity=1, distributor="x"),
                       count_date="2026-06-15T14:38:19+00:00")
    ordered = sorted([newer, older], key=lambda e: getattr(e, "count_date", "") or "")
    assert ordered[-1] is newer, "newest count_date must be applied last (wins)"


def test_warehouse_freshness_flags_stale_and_missing():
    now = datetime(2026, 6, 22, 12, 0, 0)
    fresh_dt = (now - timedelta(days=2)).isoformat()
    stale_dt = (now - timedelta(days=10)).isoformat()
    inv = {
        "a": {"distributor": "US Foods", "warehouse": "Fresh", "last_count_at": fresh_dt},
        "b": {"distributor": "US Foods", "warehouse": "Stale", "last_count_at": stale_dt},
        "c": {"distributor": "Cheney Brothers", "warehouse": "Never", "last_count_at": ""},
    }
    rows = {r["warehouse"]: r for r in warehouse_freshness(now=now, inv=inv)}
    assert rows["Fresh"]["stale"] is False
    assert rows["Stale"]["stale"] is True
    assert rows["Never"]["stale"] is True and rows["Never"]["last_count_at"] is None
    assert rows["Fresh"]["days_since_count"] == 2.0


def test_rep_map_override_resolves_without_code_change(tmpfile=None):
    import os, json, tempfile
    from integrations import rep_map
    from integrations.usfoods_inventory_report import warehouse_for_sender as usf_wfs
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        json.dump({"NewRep@usfoods.com": ["US Foods", "Tampa, FL"]}, open(path, "w"))
        os.environ["REP_MAP_FILE"] = path
        rep_map._CACHE["key"] = None  # bust cache for the test
        assert usf_wfs("New Rep <newrep@usfoods.com>") == ("US Foods", "Tampa, FL")
        # a hardcoded rep still resolves (override only ADDS)
        assert usf_wfs("maria.hernandez@usfoods.com") == ("US Foods", "Zebulon, NC")
    finally:
        os.environ.pop("REP_MAP_FILE", None)
        rep_map._CACHE["key"] = None
        os.unlink(path)


def test_inventory_audit_append_and_cap():
    import tempfile, os
    from pathlib import Path as _P
    import inventory_tracker as it
    fd, path = tempfile.mkstemp(suffix=".json"); os.close(fd)
    orig = it.INVENTORY_AUDIT_FILE
    it.INVENTORY_AUDIT_FILE = _P(path)
    try:
        it._FILE_CACHE.pop(str(path), None)
        it.append_inventory_audit([{"ts": "t1", "name": "A", "delta": 1}])
        it.append_inventory_audit([{"ts": "t2", "name": "B", "delta": 2}])
        rows = it.load_inventory_audit()
        assert rows[0]["ts"] == "t2", "newest first"
        assert len(rows) == 2
        it.append_inventory_audit([{"ts": "t%d" % i} for i in range(10)], cap=5)
        assert len(it.load_inventory_audit()) == 5, "capped"
    finally:
        it.INVENTORY_AUDIT_FILE = orig
        os.unlink(path)


def test_reconcile_po_list_splits_present_and_missing():
    from inventory_tracker import reconcile_po_list
    expected = [
        {"po_number": "3690064C", "warehouse": "La Mirada, CA"},
        {"po_number": " 014511715932 ", "warehouse": "Riviera Beach, FL"},
        {"po_number": "1087448", "warehouse": "Bronx"},
    ]
    present_set = {"014511715932", "1087448", "8513015G"}
    present, missing = reconcile_po_list(expected, present_set)
    assert [p["po_number"] for p in present] == ["014511715932", "1087448"]
    assert [m["po_number"] for m in missing] == ["3690064C"]
    # input normalized (whitespace stripped) and metadata preserved
    assert present[0]["warehouse"] == "Riviera Beach, FL"


def test_effective_last_count_prefers_stamped_over_derived_ingest_time():
    """Regression: the inventory-page warehouse chip was showing the scan day
    ("Counted Jul 3") because a usage-derived ingest timestamp overrode the
    real stamped count date whenever it was newer. Stamped must win; derived
    is a fallback only for legacy items with no stamped date."""
    from blueprints.inventory import _effective_last_count
    assert _effective_last_count("2026-06-29T19:10:28+00:00",
                                 "2026-07-03T05:06:26") == "2026-06-29T19:10:28+00:00"
    assert _effective_last_count("2026-06-29", "2026-07-03T05:06:26") == "2026-06-29"
    # fallback only when nothing is stamped
    assert _effective_last_count(None, "2026-07-03T05:06:26") == "2026-07-03T05:06:26"
    assert _effective_last_count("", "2026-07-01") == "2026-07-01"
    assert _effective_last_count(None, None) is None


def _msg(subject, body="", *, headers=None, attach=None):
    m = EmailMessage()
    m["From"] = "rep@usfoods.com"
    m["To"] = "JD@hhbagels.com"
    m["Subject"] = subject
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body or "Thanks!")
    if attach:
        fname, payload = attach
        m.add_attachment(payload, maintype="application",
                         subtype="octet-stream", filename=fname)
    return email.message_from_bytes(m.as_bytes())


def test_report_less_rep_replies_do_not_enter_unparsed_queue():
    """Regression: reps replying on the standing report thread ("I sent it over
    yesterday?", "this will be sent Mondays afternoons") were queued on
    /api/scan/health as parser gaps, burying the entries that mattered."""
    chatter = _msg("Re: Weekly Bagel Inventory & Usage Report - H&H Bagels",
                   "I sent it over yesterday?\n\nKim Cobb | Major Account Executive")
    assert _carries_report_payload(chatter, chatter["Subject"]) is False

    ooo = _msg("Automatic reply: Weekly Bagel Inventory & Usage Report",
               "I am currently out of office returning Tuesday.")
    assert _is_auto_reply(ooo, ooo["Subject"]) is True
    assert _carries_report_payload(ooo, ooo["Subject"]) is False

    header_ooo = _msg("Re: bagels", "Away.", headers={"Auto-Submitted": "auto-replied"})
    assert _is_auto_reply(header_ooo, header_ooo["Subject"]) is True


def test_real_report_payloads_still_enter_unparsed_queue():
    """The queue must still catch genuine misses: a data attachment we could
    not read, or a report table pasted into the body."""
    with_xlsx = _msg("HH Bagels Report", "Please see attached report. Ty",
                     attach=("Customer Metric Source (7).xlsx", b"PK\x03\x04junk"))
    assert _carries_report_payload(with_xlsx, with_xlsx["Subject"]) is True

    pasted = _msg("Weekly Bagel Inventory & Usage Report - 7/20/26",
                  "ITEM\tVendor#\tDescription\tCURRENT ON HAND\tON ORDER 8/5\n"
                  "1055010\t1184\tBAGEL, EGG\t23\t16")
    assert _carries_report_payload(pasted, pasted["Subject"]) is True


def test_only_report_shaped_distributor_mail_reaches_the_queue():
    """Regression: a distributor domain alone queued PO confirmations,
    vendor-portal invoices, statement reminders and delivery-appointment threads
    -- 40+ entries deep, hiding the one genuinely missed weekly report. Those
    already surface as scan errors, so they stay out of the report queue."""
    not_reports = [
        ("NoReply@usfoods.com", "Confirm USF PO 256295 5G 07/22/26 H&H BAGELS"),
        ("noreply@usfoods.com", "US Foods Vendor Portal Invoice"),
        ("Statements.Shared@usfoods.com", "Reminder - Statement Request - 0000670701"),
        ("Liz.Cantillo@usfoods.com", "Re: Due 7/23 - Missing DEL APPTs - PO#2055126H"),
        ("Ari.Gonzales@usfoods.com", "RE: US Foods Austin Onboarding Request ID# 35078882"),
        ("JDoyle@chefswarehouse.com", "RE: H&H Bagels - Past Due invoice"),
        ("Matthew.Greene@usfoods.com", "RE: Bagels"),
    ]
    for sender, subject in not_reports:
        assert _is_report_shaped(sender, subject) is False, (sender, subject)

    # A mapped report rep always qualifies -- their format changes are exactly
    # what the queue exists to surface, whatever they title the mail.
    assert _is_report_shaped("Kimberly.Cobb@usfoods.com", "July 27 Report") is True
    # An unmapped sender still qualifies on a report-shaped subject/filename,
    # so a new DC rep's first report is never silently dropped.
    assert _is_report_shaped("newrep@usfoods.com", "HH Bagels Product Usage Report") is True
    assert _is_report_shaped("newrep@usfoods.com", "FW: numbers",
                             ["Customer Metric Source (7).xlsx"]) is True


def test_prune_unparsed_drops_self_healed_and_aged_out_entries():
    """The queue is a to-do list, not a log: an entry clears once its warehouse
    counts after the unreadable message, or once it ages out."""
    import inventory_tracker as it

    now = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)
    entries = [
        # self-healed: Manassas counted 7/27 18:00, after this 14:18 message
        {"id": "healed", "sender": "Jasmin.Gomez@usfoods.com",
         "subject": "HH Bagels Report", "received": "2026-07-27T14:18:13Z"},
        # aged out: 30 days old
        {"id": "old", "sender": "maria.hernandez@usfoods.com",
         "subject": "RE: report", "received": "2026-06-28T15:00:00Z"},
        # still open: recent, and Zebulon's last count predates it
        {"id": "open", "sender": "maria.hernandez@usfoods.com",
         "subject": "Weekly Bagel Inventory & Usage Report",
         "received": "2026-07-28T15:28:22Z"},
        # unmapped sender, recent -> keep (can't prove it healed)
        {"id": "unknown", "sender": "someone@cheneybrothers.com",
         "subject": "stock sheet", "received": "2026-07-28T09:00:00Z"},
    ]
    freshness = [
        {"warehouse": "Manassas, VA", "last_count_at": "2026-07-27T18:00:00+00:00"},
        {"warehouse": "Zebulon, NC", "last_count_at": "2026-07-20T18:25:21+00:00"},
    ]

    stored = {}
    orig_load, orig_write = it.load_unparsed_reports, it._write_json
    try:
        it.load_unparsed_reports = lambda: list(entries)
        it._write_json = lambda path, data: stored.setdefault("data", data)
        kept = it.prune_unparsed_reports(now=now, freshness=freshness)
    finally:
        it.load_unparsed_reports, it._write_json = orig_load, orig_write

    assert [e["id"] for e in kept] == ["open", "unknown"], kept
    assert stored["data"] == kept


def test_prune_unparsed_clears_non_report_backlog_and_own_count():
    """The historical backlog must clear itself: entries queued before the
    scanner could classify them are re-judged from sender + subject. And an
    entry whose own report supplied the count (stamped with its send time, so
    equal not newer) is healed -- an off-by-one here kept it queued forever."""
    import inventory_tracker as it

    now = datetime(2026, 7, 28, 17, 40, tzinfo=timezone.utc)
    entries = [
        {"id": "po", "sender": "NoReply@usfoods.com",
         "subject": "Confirm USF PO 256295 5G 07/22/26 H&H BAGELS",
         "received": "2026-07-27T16:43:49Z"},
        {"id": "invoice", "sender": "noreply@usfoods.com",
         "subject": "US Foods Vendor Portal Invoice",
         "received": "2026-07-23T16:45:02Z"},
        {"id": "ooo", "sender": "mross@cheneybrothers.com",
         "subject": "Automatic reply: Weekly Bagel Inventory & Usage Report",
         "received": "2026-07-20T15:39:11Z"},
        # its own report supplied Manassas' count -> equal timestamps -> healed
        {"id": "own-count", "sender": "Jasmin.Gomez@usfoods.com",
         "subject": "HH Bagels Report", "received": "2026-07-27T14:18:13Z"},
        {"id": "live", "sender": "Kimberly.Cobb@usfoods.com",
         "subject": "July 27 Report", "received": "2026-07-28T16:00:00Z"},
    ]
    freshness = [
        {"warehouse": "Manassas, VA", "last_count_at": "2026-07-27T14:18:13Z"},
        {"warehouse": "Alcoa, TN", "last_count_at": "2026-07-27T19:03:46+00:00"},
    ]

    orig_load, orig_write = it.load_unparsed_reports, it._write_json
    try:
        it.load_unparsed_reports = lambda: list(entries)
        it._write_json = lambda path, data: None
        kept = it.prune_unparsed_reports(now=now, freshness=freshness)
    finally:
        it.load_unparsed_reports, it._write_json = orig_load, orig_write

    assert [e["id"] for e in kept] == ["live"], kept


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"ok: {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"FAIL: {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
