"""A scan that stops at the message or page cap says so, and a caller that
names no window gets the cron's (review 2026-11-12, item 9)."""
from integrations import email_scanner as ES

from ingest_fakes import FakeGraph, mime, route_scan, scan_with

REP = "maria.hernandez@usfoods.com"


def _msgs(n):
    return [{"id": f"m{i}", "sender": REP, "subject": "thanks",
             "mime": mime(REP, "thanks", text="ok", msgid=f"<m{i}@x>")}
            for i in range(n)]


def test_message_budget_truncation_is_flagged(monkeypatch):
    res = scan_with(monkeypatch, FakeGraph(_msgs(7), page_size=3), max_messages=4)
    assert res.truncated == ["jd@ms.hhbagels.com"]
    err = next(e for e in res.errors if e.startswith("scan truncated: "))
    assert "4-message budget" in err and ES.failure_class(err) == "truncated"


def test_page_cap_truncation_is_flagged(monkeypatch):
    monkeypatch.setattr(ES, "_MAX_LIST_PAGES", 2)
    noise = [{"id": f"n{i}", "sender": "news@example.com", "subject": "x",
              "mime": mime("news@example.com", "x")} for i in range(9)]
    res = scan_with(monkeypatch, FakeGraph(noise, page_size=3), max_messages=50)
    assert res.truncated and "2-page listing cap" in res.errors[-1]


def test_complete_scan_is_not_truncated(monkeypatch):
    res = scan_with(monkeypatch, FakeGraph(_msgs(4), page_size=3), max_messages=4)
    assert res.truncated == [] and not any("truncated" in e for e in res.errors)
    # Budget hit exactly, but only non-candidates remain on the page.
    msgs = _msgs(2) + [{"id": "n", "sender": "news@example.com", "subject": "x",
                        "mime": mime("news@example.com", "x")}]
    res = scan_with(monkeypatch, FakeGraph(msgs), max_messages=2)
    assert res.truncated == []


def test_route_marks_partial_and_reports_truncation(tmp_path, monkeypatch):
    res = ES.ScanResult(source="ms365", messages_seen=4000)
    res.truncated.append("info@ms.hhbagels.com")
    res.errors.append("scan truncated: info@ms.hhbagels.com stopped at the "
                      "200-message budget with older mail in the window unread")
    c, r, _ = route_scan(monkeypatch, tmp_path, res)
    body = r.get_json()
    rep = body["reports"][0]
    assert rep["status"] == "partial" and rep["truncated"] == ["info@ms.hhbagels.com"]
    assert [f["key"].split(":")[0] for f in body["new_failures"]] == ["truncated"]
    h = c.get("/api/scan/health", headers={"X-Inventory-Token": "tok"}).get_json()
    assert h["last_scan"]["truncated"] == ["info@ms.hhbagels.com"]


def test_scan_now_without_a_window_gets_14_days_and_200(tmp_path, monkeypatch):
    _c, _r, seen = route_scan(monkeypatch, tmp_path, ES.ScanResult(source="ms365"),
                              body={"dry_run": True})
    assert seen["max_messages"] == 200
    assert seen["filter_override"].startswith("receivedDateTime ge ")
    # An explicit window is left alone.
    _c, _r, seen = route_scan(monkeypatch, tmp_path, ES.ScanResult(source="ms365"),
                              body={"max_messages": 25})
    assert seen["max_messages"] == 25 and seen["filter_override"] is None
