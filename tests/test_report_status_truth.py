"""/report-status tells the truth (review 2026-10-22):
  * a Graph error for a rep is "unknown", not "missing", and is not cached;
  * an email that arrived but left no count is "received, not ingested";
  * the Manassas customer-contact alias the scanner reads is on the roster."""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from integrations import report_status as RS
from integrations.usfoods_inventory_report import REPORT_SENDER_TO_WAREHOUSE

NOW = datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)   # a Tuesday
SHEET = {"attachments": [{"name": "Report.xlsx", "isInline": False,
                          "contentType": "application/vnd.ms-excel"}]}


def _msg(received, subject="Weekly Bagel Inventory & Usage Report"):
    return {**SHEET, "subject": subject, "receivedDateTime": received, "body": {}}


def _run(monkeypatch, replies, counts, failing=()):
    monkeypatch.setenv("MS365_TENANT_ID", "t")
    monkeypatch.setenv("MAILBOXES", "JD@ms.hhbagels.com")
    monkeypatch.setattr(RS, "_token", lambda: "tok")

    def rep_messages(token, mailbox, rep_email, since_iso, top=10):
        if rep_email in failing:
            raise RuntimeError("Graph 503")
        return replies.get(rep_email, [])
    monkeypatch.setattr(RS, "_rep_messages", rep_messages)
    monkeypatch.setattr(RS, "_last_count_by_warehouse", lambda: counts)
    return RS.compute_report_status(now=NOW)


def _by_wh(res):
    return {w["warehouse"]: w["status"] for w in res["warehouses"]}


def test_query_error_is_unknown_not_missing_and_not_cached(monkeypatch, tmp_path):
    res = _run(monkeypatch, {}, {}, failing={"michael.via@usfoods.com"})
    st = _by_wh(res)
    assert st["Chicago, IL"] == "unknown"
    assert st["Zebulon, NC"] == "missing"
    assert res["unknown"] == 1 and "michael.via@usfoods.com" in res["query_errors"]
    html = RS.render_html(res)
    assert "unknown" in html and "1 unknown (mailbox query failed)" in html
    # get_status: an "unknown" result is returned but never cached / written.
    writes = []
    monkeypatch.setattr(RS, "compute_report_status", lambda now=None: res)
    monkeypatch.setattr(RS, "_write_disk", lambda data: writes.append(data))
    monkeypatch.setitem(RS._CACHE, "data", None)
    monkeypatch.setitem(RS._CACHE, "at", 0.0)
    RS.get_status(force=True)
    assert writes == [] and RS._CACHE["data"] is None


def test_received_but_not_ingested(monkeypatch):
    replies = {"jasmin.gomez@usfoods.com": [_msg("2026-10-19T14:12:42Z")],
               "maria.hernandez@usfoods.com": [_msg("2026-10-19T18:07:50Z")],
               "sam.travlos@usfoods.com": [_msg("2026-10-19T14:34:40Z")]}
    counts = {
        "Manassas, VA": "2026-10-12T13:16:22Z",   # last week's: this one failed
        "Zebulon, NC": "2026-10-19T18:07:43Z",    # stamped with the send time
        "La Mirada, CA": "2026-10-19",            # subject count date (date-only)
    }
    res = _run(monkeypatch, replies, counts)
    st = _by_wh(res)
    assert st["Manassas, VA"] == "received_not_ingested"
    assert st["Zebulon, NC"] == "received"
    assert st["La Mirada, CA"] == "received"
    assert res["not_ingested"] == 1 and res["received"] == 3
    assert "received, not ingested" in RS.render_html(res)


def test_no_freshness_means_no_cross_check(monkeypatch):
    res = _run(monkeypatch, {"jasmin.gomez@usfoods.com": [_msg("2026-10-19T14:12:42Z")]},
               None)
    assert _by_wh(res)["Manassas, VA"] == "received"


def test_manassas_alias_is_on_the_roster_and_never_chased():
    assert REPORT_SENDER_TO_WAREHOUSE["usfcustomercontact@usfoods.com"][1] == "Manassas, VA"
    reps = {r["email"]: r for r in RS.WAREHOUSE_REPS["Manassas, VA"]}
    assert reps["usfcustomercontact@usfoods.com"].get("no_chase") is True
