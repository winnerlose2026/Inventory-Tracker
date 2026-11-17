"""A delivery confirmation must be read wherever Lineage writes it (JD 11/17:
PO 9919425G showed "in transit" for hours after Belinda wrote
"9919425G-delivered").

The miss: JD had replied on the Quote and Schedule thread with an edited
subject ("... - ETAs for 9751114C and 9919425G"). Outlook starts a NEW
conversation for that; it holds no request blocks, so the sync dropped it and
the confirmation on it was never read. Now such a fork -- and any thread whose
subject names a held PO -- is read as an evidence thread.
"""
import sys
from datetime import timedelta

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from test_po_delivery import BELINDA, NOW, REQUEST_1012, _inv, _routes
from test_delivery_server_confirm import _scheduled

ISO = lambda h: (NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731


def _msg(mid, h, sender, text, subject, conv):
    return {"id": mid, "subject": subject, "conversationId": conv,
            "sentDateTime": ISO(h), "internetMessageId": f"<{mid}>",
            "from": {"emailAddress": {"address": sender}},
            "toRecipients": [], "ccRecipients": [],
            "uniqueBody": {"content": text}}


def _run(monkeypatch, msgs, dry_run=False):
    import blueprints.email as E
    c, store = _routes(monkeypatch, _inv(eta="2026-10-10T00:00:00"),
                       {"393072B2": _scheduled()}, synced={},
                       extra_ledger={"014511763291": {
                           "po_number": "014511763291", "status": "in_transit",
                           "distributor": "Cheney Brothers",
                           "warehouse": "Riviera Beach, FL", "total_cs": 336.0}})
    monkeypatch.setenv("MS365_USER", "JD@ms.hhbagels.com")
    monkeypatch.setattr(E, "_graph_token", lambda: "g")
    queries = []

    def graph(url, token, extra=None, timeout=30):
        queries.append(url)
        return {"value": msgs}
    monkeypatch.setattr(E, "_graph_json", graph)
    out = c.post("/api/pos/delivery/sync-threads", json={"dry_run": dry_run},
                 headers={"X-Inventory-Token": "tok"}).get_json()
    assert out["ok"], out
    return out, store, queries


BASE = "RE: Quote and Schedule - 2 Orders"


def test_confirmation_on_a_renamed_quote_and_schedule_reply_is_applied(monkeypatch):
    fork = BASE + " - ETAs for 014511763291 and 393072B2"
    msgs = [
        _msg("req", 70, "Isaiah@hhbagels.com", REQUEST_1012, BASE, "conv1"),
        _msg("ask", 3, "JD@hhbagels.com",
             "Hi Rojean, Belinda,\n\n  *   PO 014511763291 (Cheney Riviera Beach): "
             "Lineage has the freight.\n  *   PO 393072B2 (USF Houston): Bozel has "
             "the freight.\n\nCould you send the current ETA for each?\n\nThanks,\nJD",
             fork, "conv2"),
        _msg("ans", 2, BELINDA, "Noted.\n014511763291-0830AM/20th\n393072B2-delivered\n"
             "Belinda Morris", fork, "conv2"),
    ]
    out, store, _q = _run(monkeypatch, msgs)
    got = [g for g in out["confirmed_by_server"] if g["applied"]]
    assert [g["po_number"] for g in got] == ["393072B2"], out["confirmed_by_server"]
    rec = store["tracking"]["393072B2"]
    assert rec["status"] == "delivered"
    # undated "delivered" after a passed appointment: dated to the appointment day
    assert rec["delivered_at"].startswith("2026-10-15"), rec["delivered_at"]
    th = store["threads"]["threads"]["conv2"]
    assert th["evidence_only"] and "393072B2" in th["po_numbers"]
    # the original thread is still the one follow-ups reply on
    assert not store["threads"]["threads"]["conv1"].get("evidence_only")


def test_a_thread_named_for_the_po_is_read_too(monkeypatch):
    other = "PO 393072B2 - Houston"
    msgs = [
        _msg("req", 70, "Isaiah@hhbagels.com", REQUEST_1012, BASE, "conv1"),
        _msg("ans", 2, BELINDA, "This load delivered 10/15\n\nBelinda Morris",
             other, "conv9"),
    ]
    out, store, queries = _run(monkeypatch, msgs)
    assert any("subject%3A393072B2" in q for q in queries), queries
    rec = store["tracking"]["393072B2"]
    assert rec["status"] == "delivered" and rec["delivered_at"].startswith("2026-10-15")


def test_a_fork_that_names_no_tracked_po_changes_nothing(monkeypatch):
    fork = BASE + " - quick question"
    msgs = [
        _msg("req", 70, "Isaiah@hhbagels.com", REQUEST_1012, BASE, "conv1"),
        _msg("ans", 2, BELINDA, "Delivered 0752AM/15th\nBelinda Morris", fork, "conv2"),
    ]
    out, store, _q = _run(monkeypatch, msgs)
    assert store["tracking"]["393072B2"]["status"] == "scheduled"
    assert "conv2" not in store["threads"]["threads"]
