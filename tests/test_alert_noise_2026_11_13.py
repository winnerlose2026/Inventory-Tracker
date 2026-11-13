"""The 2026-11-13 morning digest listed 13 problems; one was real.

Each test pins one of the false alarms (or the one real fault) so it cannot
come back:

  * Cheney accounts-payable "Check #  $0.00" mail -- an ACH remittance advice
    with a "PO Number" column -- was read as a dropped purchase order;
  * one-line replies on the report thread ("I will review with purchasing
    tomorrow") were queued as unread reports because the QUOTED thread says
    "weekly usage", and a rep covering three warehouses never self-healed;
  * an entry the prune had just retired was still raised as an issue, and a
    download failure was raised twice;
  * a Graph 502 on folder ids / one message download waited for a full,
    clean 14-day scan to clear;
  * a production sheet sent with a blank subject and re-sent labelled six
    minutes later stayed as a second, unassigned sheet with an alert;
  * a Microsoft 503 on the info@ subscription renew deleted nothing only
    because the DELETE also 503'd, and nothing retried for twelve hours.
"""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

import inventory_tracker as it
import integrations.email_scanner as es
import integrations.graph_subscriptions as gs
import integrations.production_store as ps
from core import feed_health

REMITTANCE_TEXT = (
    "1026737 10/07/2026 4511772161 1,484.00 0.00 1,484.00 Total $ 1,484.00 "
    "Vendor Address H & H BAGELS Invoice Number Inv Date PO Number Gross "
    "Amount Discount Net Amount ACH Remittance Advice Remittance Address "
    "Payment Date: 11/13/2026 Notification: We have settled the items listed "
    "below with bank transfer number")


# --- Cheney check copies are not purchase orders ------------------------------

def test_remittance_advice_text_is_not_a_po():
    assert es._PDF_NOT_PO_RE.search(REMITTANCE_TEXT)
    assert es._PO_TEXT_RE.search(REMITTANCE_TEXT)      # why it misfired before


def test_check_copy_subject_raises_no_dropped_po_error(monkeypatch):
    monkeypatch.setattr(es, "_pdf_text_looks_like_po", lambda b: True)
    po = SimpleNamespace(lines=[], po_number="")
    assert es._zero_line_po_error(po, b"%PDF", "Check #  $0.00",
                                  "0000863375_2002731830_0.PDF") is None


def test_a_real_zero_line_po_still_raises(monkeypatch):
    monkeypatch.setattr(es, "_pdf_text_looks_like_po", lambda b: True)
    po = SimpleNamespace(lines=[], po_number="")
    err = es._zero_line_po_error(po, b"%PDF", "Cheney Brothers PO", "po.pdf")
    assert err and es.is_po_failure(err)


def test_pdf_text_with_remittance_markers_is_not_a_po(monkeypatch):
    class _Page:
        def extract_text(self):
            return REMITTANCE_TEXT

    class _Reader:
        def __init__(self, _b):
            self.pages = [_Page()]

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", _Reader)
    assert es._pdf_text_looks_like_po(b"%PDF") is False


# --- a reply's quoted thread is not a payload ----------------------------------

def _reply(subject, own, quoted=""):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = "Michael Ross <mross@cheneybrothers.com>"
    body = own + ("\n\nOn Nov 1, 2026, at 9:33 AM, JD Gross <jd@hhbagels.com> wrote:\n"
                  + quoted if quoted else "")
    m.set_content(body)
    return m


def test_one_line_reply_quoting_the_report_is_not_a_payload():
    msg = _reply("Re: Weekly Usage and Stock @ Cheney Facilities",
                 "Good morning, I will review with purchasing tomorrow",
                 "Attached please find weekly usage and current inventory")
    assert es._carries_report_payload(msg, msg["Subject"]) is False


def test_reply_with_its_own_table_marker_is_a_payload():
    msg = _reply("Re: Weekly Usage and Stock @ Cheney Facilities",
                 "ITEM  CS OH  WKLY USE\n1055010  12  5")
    assert es._carries_report_payload(msg, msg["Subject"]) is True


def test_a_forward_keeps_its_quoted_payload():
    msg = _reply("FW: Product Usage Report", "see below",
                 "Current on hand 12 cases")
    assert es._carries_report_payload(msg, msg["Subject"]) is True


# --- a multi-warehouse rep self-heals once every warehouse counted ---------------

def _ross_entry(sent):
    return {"id": "m1", "kind": "report", "mailbox": "JD@ms.hhbagels.com",
            "sender": "mross@cheneybrothers.com",
            "subject": "Re: Weekly Usage and Stock @ Cheney Facilities",
            "received": sent, "sent": sent, "filenames": []}


def _fresh(ts_by_wh):
    return [{"warehouse": wh, "last_count_at": ts} for wh, ts in ts_by_wh.items()]


def test_ross_reply_is_retired_when_all_three_facilities_counted_since():
    now = datetime(2026, 11, 13, 15, tzinfo=timezone.utc)
    it._write_json(it.UNPARSED_REPORTS_FILE, [_ross_entry("2026-11-02T13:51:37Z")])
    kept = it.prune_unparsed_reports(now=now, freshness=_fresh({
        "Riviera Beach, FL": "2026-11-09T18:25:00Z",
        "Ocala, FL": "2026-11-09T14:20:00Z",
        "Punta Gorda, FL": "2026-11-09T14:20:00Z"}))
    assert kept == []


def test_ross_reply_stays_while_one_facility_has_not_counted():
    now = datetime(2026, 11, 13, 15, tzinfo=timezone.utc)
    it._write_json(it.UNPARSED_REPORTS_FILE, [_ross_entry("2026-11-02T13:51:37Z")])
    kept = it.prune_unparsed_reports(now=now, freshness=_fresh({
        "Riviera Beach, FL": "2026-11-09T18:25:00Z",
        "Ocala, FL": "2026-10-30T14:20:00Z",
        "Punta Gorda, FL": "2026-11-09T14:20:00Z"}))
    assert [e["id"] for e in kept] == ["m1"]


# --- scan issues: retired entries and duplicates are not raised ------------------

def test_retired_report_entry_is_not_raised_and_fetch_failure_is_said_once():
    from blueprints.email import _collect_scan_issues
    fetch_err = es.fetch_failed_error("JD@ms.hhbagels.com", "RE: thing", "a@usfoods.com")
    scan = SimpleNamespace(
        issues=[{"key": "fetch-failed:<x>", "message": "could not be downloaded",
                 "severity": "error"}],
        unparsed=[{"id": "<tom>", "kind": "report", "subject": "RE: Weekly",
                   "sender": "tom.foley@usfoods.com", "mailbox": "JD"},
                  {"id": "<kept>", "kind": "report", "subject": "RE: Weekly",
                   "sender": "x@usfoods.com", "mailbox": "JD"}])
    issues = _collect_scan_issues({"errors": [fetch_err]}, scan,
                                  queued_ids={"<kept>"})
    keys = [i["key"] for i in issues]
    assert "unparsed:<tom>" not in keys and "unparsed:<kept>" in keys
    assert [k for k in keys if k.startswith("fetch-failed")] == ["fetch-failed:<x>"]


def test_folders_and_fetch_issues_clear_on_the_next_scan_that_proves_them_fine():
    from blueprints.email import _clear_proved_resolved
    fkey = es.scan_error_issue_key(es.folders_error("info@ms.hhbagels.com"))
    assert fkey.startswith("folders:")
    feed_health.raise_issue(fkey, "inbox only", "warn")
    feed_health.raise_issue("fetch-failed:<louis>", "not downloaded", "error")
    feed_health.raise_issue("folders:other", "still broken", "warn")
    scan = SimpleNamespace(resolved_keys=[fkey, "fetch-failed:<louis>"])
    _clear_proved_resolved(scan, issues=[])
    open_now = feed_health.snapshot()["issues"]
    assert fkey not in open_now and "fetch-failed:<louis>" not in open_now
    assert "folders:other" in open_now


def test_an_issue_raised_again_by_the_same_scan_is_not_cleared():
    from blueprints.email import _clear_proved_resolved
    feed_health.raise_issue("fetch-failed:<y>", "x", "error")
    _clear_proved_resolved(SimpleNamespace(resolved_keys=["fetch-failed:<y>"]),
                           issues=[{"key": "fetch-failed:<y>"}])
    assert "fetch-failed:<y>" in feed_health.snapshot()["issues"]


# --- a re-sent production sheet replaces its unlabelled first copy --------------

def _sheet(mid, subject, recv, *, wh="", po=""):
    return {"source_message_id": mid, "source_subject": subject,
            "source_sender": "Gabo@hhbagels.com", "received_at": recv,
            "production_date": "2026-11-10", "warehouse": wh, "po_number": po,
            "lines": [{"lot_number": "1150111126", "variety": "Plain", "cs_count": 79},
                      {"lot_number": "1152111226", "variety": "Poppy Seed", "cs_count": 32}],
            "warning": "" if wh else "could not tell which distributor/warehouse"}


BLANK = ("<blank>", "Daily Production ", "2026-11-13T12:54:24Z")
LABELLED = ("<rvb>", "Daily Production Sheet RIVIERA BEACH PO.014511780511",
            "2026-11-13T13:00:57Z")


def test_labelled_resend_replaces_the_unlabelled_copy_and_clears_its_alert():
    records = []
    assert ps.ingest_production_record(records, _sheet(*BLANK))[0] == "ingested"
    feed_health.raise_issue(ps.sheet_issue_key("<blank>"), "needs a look", "warn")
    status, stored = ps.ingest_production_record(
        records, _sheet(*LABELLED, wh="Riviera Beach, FL", po="014511780511"))
    assert status == "replaced" and len(records) == 1
    assert "<blank>" in stored["replaced"]
    ps._report_health([(status, stored)], "", "")
    assert ps.sheet_issue_key("<blank>") not in feed_health.snapshot()["issues"]
    # the blank copy is never stored again
    assert ps.ingest_production_record(records, _sheet(*BLANK))[0] == "duplicate"


def test_unlabelled_copy_arriving_second_is_superseded():
    records = []
    ps.ingest_production_record(
        records, _sheet(*LABELLED, wh="Riviera Beach, FL", po="014511780511"))
    status, _ = ps.ingest_production_record(records, _sheet(*BLANK))
    assert status == "superseded" and len(records) == 1


def test_stored_pair_is_collapsed_once():
    records = [_sheet(*BLANK),
               _sheet(*LABELLED, wh="Riviera Beach, FL", po="014511780511")]
    assert ps.collapse_unlabelled_resends(records) == ["<blank>"]
    assert [r["source_message_id"] for r in records] == ["<rvb>"]


def test_different_lines_are_not_a_resend():
    blank = _sheet(*BLANK)
    blank["lines"] = blank["lines"][:1]
    records = [blank, _sheet(*LABELLED, wh="Riviera Beach, FL", po="014511780511")]
    assert ps.collapse_unlabelled_resends(records) == []
    assert len(records) == 2


# --- Microsoft 503 on renew: keep the subscription, warn, retry ------------------

@pytest.fixture
def subs_env(monkeypatch):
    monkeypatch.setenv("GRAPH_SUB_CLIENT_STATE", "cs")
    monkeypatch.setenv("GRAPH_WEBHOOK_BASE", "https://example.onrender.com")
    monkeypatch.setenv("MS365_USER", "jd@hhbagels.com,info@hhbagels.com")
    monkeypatch.setattr(gs.EmailInboxClient, "_ms365_token", lambda self: "tok")
    monkeypatch.setattr(gs, "_RETRY_PAUSE_S", 0)
    exp = (datetime.now(timezone.utc) + timedelta(hours=40)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    gs._save_state({"subscriptions": [
        {"id": "jd-sub", "user": "jd@hhbagels.com", "expiration_date_time": exp},
        {"id": "info-sub", "user": "info@hhbagels.com", "expiration_date_time": exp}]})


def _flaky(calls, *, fail_user_sub="info-sub", status=503):
    def fake(method, url, token, body=None):
        calls.append((method, url))
        if fail_user_sub in url or (method == "POST"):
            raise RuntimeError(f"Graph {method} {url} failed: {status} "
                               "Service Unavailable: {}")
        return {"expirationDateTime": "2099-01-01T00:00:00Z"}
    return fake


def test_503_keeps_the_subscription_retries_once_and_warns(subs_env, monkeypatch):
    calls = []
    monkeypatch.setattr(gs, "_graph_request", _flaky(calls))
    out = gs.renew_subscriptions()
    methods = [m for m, u in calls if "info-sub" in u or m == "POST"]
    assert methods == ["PATCH", "PATCH"]               # retried, never deleted
    bad = [r for r in out["results"] if not r["ok"]]
    assert len(bad) == 1 and bad[0]["transient"] and "still valid" in bad[0]["error"]
    assert {s["id"] for s in gs.list_subscriptions()} == {"jd-sub", "info-sub"}
    issues = feed_health.snapshot()["issues"]
    assert issues["graph-subscriptions:renew"]["severity"] == "warn"
    assert gs.renew_failure_pending() is True
    # Microsoft recovers: the hourly retry clears it.
    monkeypatch.setattr(gs, "_graph_request", lambda *a, **k: {
        "expirationDateTime": "2099-01-01T00:00:00Z"})
    assert gs.renew_subscriptions()["ok"] is True
    assert gs.renew_failure_pending() is False


def test_403_still_recreates(subs_env, monkeypatch):
    calls = []

    def fake(method, url, token, body=None):
        calls.append(method)
        if method == "PATCH" and "info-sub" in url:
            raise RuntimeError(f"Graph PATCH {url} failed: 403 Forbidden: {{}}")
        return {"id": "new", "expirationDateTime": "2099-01-01T00:00:00Z"}
    monkeypatch.setattr(gs, "_graph_request", fake)
    out = gs.renew_subscriptions()
    assert out["ok"] is True and "DELETE" in calls and "POST" in calls


def test_little_time_left_escalates_under_its_own_key(subs_env, monkeypatch):
    soon = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    st = gs._load_state()
    for s in st["subscriptions"]:
        s["expiration_date_time"] = soon
    gs._save_state(st)
    monkeypatch.setattr(gs, "_graph_request", _flaky([]))
    gs.renew_subscriptions()
    issues = feed_health.snapshot()["issues"]
    assert issues["graph-subscriptions:renew-urgent"]["severity"] == "error"


def test_watchdog_does_not_repeat_the_renew_failure():
    from integrations import watchdog
    feed_health.raise_issue("graph-subscriptions:renew", "503", "warn")
    view = {"feeds": {"graph-renew": {"ok": False, "stale": True, "last_ok": "",
                                      "last_attempt": "", "detail": "",
                                      "max_age_hours": 26}},
            "issues": [{"key": "graph-subscriptions:renew"}]}
    watchdog._check_feeds(view)
    assert not [k for k in feed_health.snapshot()["issues"]
                if k.startswith("feed-")]


def test_stale_wording_for_a_feed_that_never_succeeded():
    from integrations import watchdog
    view = {"feeds": {"toast-nightly": {"ok": False, "stale": True, "last_ok": "",
                                        "last_attempt": "", "detail": "",
                                        "max_age_hours": 26}},
            "issues": []}
    watchdog._check_feeds(view)
    msg = feed_health.snapshot()["issues"]["feed-stale:toast-nightly"]["message"]
    assert "since never" not in msg and "successfully yet" in msg
