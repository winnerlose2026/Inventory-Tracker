"""A PO PDF that parses to zero lines is an error, a queue entry and an issue
(review 2026-11-12, item 1). It used to return events=[] errors=[] and vanish."""
import inventory_tracker as it
from integrations import email_scanner as ES
from integrations.usfoods_po_parser import UsFoodsPO
from integrations.cheney_po_parser import CheneyPO

from ingest_fakes import FakeGraph, PDF, mime, route_scan, scan_with


def test_usf_zero_line_po_is_a_po_failure(monkeypatch):
    monkeypatch.setattr(ES, "_usfoods_parse_po_pdf", lambda b: UsFoodsPO())
    events, errors = ES._usfoods_po_to_events(
        b"%PDF", "US Foods", "<m>", "US Foods PO Request - 7135734C",
        filename="US Foods PO Request - 7135734C - Date 112026.PDF")
    assert events == []
    assert len(errors) == 1 and "could not be read -- 0 lines" in errors[0]
    assert ES.is_po_failure(errors[0])


def test_cheney_zero_line_po_is_a_po_failure(monkeypatch):
    monkeypatch.setattr(ES, "_cheney_parse_po_pdf",
                        lambda b: CheneyPO(po_number="054511767347"))
    events, errors = ES._cheney_po_to_events(b"%PDF", "Cheney Brothers", "<m>", "hello")
    assert events == [] and ES.is_po_failure(errors[0])
    assert "054511767347" in errors[0]


def test_non_po_pdf_with_no_lines_stays_quiet(monkeypatch):
    """An invoice / statement PDF from the same sender is not a dropped PO."""
    monkeypatch.setattr(ES, "_usfoods_parse_po_pdf", lambda b: UsFoodsPO())
    events, errors = ES._usfoods_po_to_events(
        b"not a pdf", "US Foods", "<m>", "Vendor portal invoice 4411",
        filename="Invoice_4411.pdf")
    assert events == [] and errors == []


def test_scan_queues_the_message_and_raises_an_issue(monkeypatch):
    monkeypatch.setattr(ES, "_usfoods_parse_po_pdf", lambda b: UsFoodsPO())
    msg = mime("buyer@usfoods.com", "US Foods PO Request - 7135734C",
               attachments=[("US Foods PO Request - 7135734C.PDF", b"%PDF-1.4", PDF)])
    g = FakeGraph([{"id": "g1", "sender": "buyer@usfoods.com",
                    "subject": "US Foods PO Request - 7135734C", "mime": msg}])
    res = scan_with(monkeypatch, g)
    assert res.events == []
    assert any(ES.is_po_failure(e) for e in res.errors)
    assert [u["kind"] for u in res.unparsed] == ["po"]
    assert res.unparsed[0]["id"] == "<g1@x>"
    assert [i["key"] for i in res.issues] == ["po-parse:<g1@x>"]


def test_route_persists_queue_entry_and_feed_health_issue(tmp_path, monkeypatch):
    from core import feed_health
    err = "PO PDF 'US Foods PO Request - 7135734C': could not be read -- 0 lines"
    res = ES.ScanResult(source="ms365", messages_seen=1, errors=[err])
    res.unparsed.append({"kind": "po", "reason": err, "id": "<g1@x>",
                         "mailbox": "jd@ms.hhbagels.com",
                         "sender": "buyer@usfoods.com",
                         "subject": "US Foods PO Request - 7135734C",
                         "filenames": ["po.pdf"],
                         "received": "2026-11-09T14:00:05Z", "sent": ""})
    res.issues.append({"key": "po-parse:<g1@x>", "message": err, "severity": "error"})
    _c, r, _ = route_scan(monkeypatch, tmp_path, res)
    rep = r.get_json()["reports"][0]
    assert rep["status"] == "partial" and rep["po_failures"] == [err]
    assert [u["id"] for u in it.load_unparsed_reports()] == ["<g1@x>"]
    assert "po-parse:<g1@x>" in feed_health.snapshot()["issues"]
