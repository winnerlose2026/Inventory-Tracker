"""Production tab hardening (review 2026-11-12):

  * lines were dropped silently and the header total was never compared with
    the lines (336 printed, 112 stored);
  * "This week" was the last week that HAD data, so a ten-week gap looked fine;
  * an unreadable sheet became a permanent hidden stub;
  * the catch-up scan read the Inbox only, the first PDF only, and called a
    scan that stopped at its page cap "complete".
"""
import base64
import json
import sys
from datetime import date

sys.path.insert(0, ".")

import pytest

import inventory_tracker as it
import integrations.production_store as ps
from core import feed_health
from integrations.production_pdf_parser import parse_production_text


@pytest.fixture
def datadir(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    monkeypatch.setattr(it, "PRODUCTION_FILE", tmp_path / "production.json")
    monkeypatch.setattr(ps, "ledger_po_index", lambda ledger=None: {})
    it._FILE_CACHE.clear()
    yield tmp_path
    it._FILE_CACHE.clear()


CLEAN = ("CS Count SKU\n56 CS PARB-PLAIN\n56 CS PARB-ONION\n"
         "112 Total CasesOCALA.PO.054511751617\nLot#\n")


# --- parser ------------------------------------------------------------------

def test_line_regex_tolerates_how_people_actually_type():
    text = ("CS Count SKU\n56 cs Parb-Plain\n1,120 CS PARB-EVERYTHING\n40CS PARB-ONION\n"
            "24 CS JALAPENO/CHEDDAR\n16 CS CINN. RAISIN\n"
            "1256 Total CasesOCALA.PO.054511751617\n")
    s = parse_production_text(text)
    got = {L.variety: L.cs_count for L in s.lines}
    assert got == {"Plain": 56, "Everything": 1120, "Onion": 40,
                   "Jalapeno Cheddar": 24, "Cinnamon Raisin": 16}
    assert s.line_total_cases == 1256 and s.header_total_cases == 1256
    assert s.warnings == [] and s.warning == ""


def test_header_total_that_does_not_match_the_lines_is_flagged():
    text = ("CS Count SKU\n56 CS PARB-PLAIN\n56 CS PARB-ONION\n"
            "336 Total CasesOCALA.PO.054511751617\n")
    s = parse_production_text(text)
    assert s.total_cases == 336 and s.line_total_cases == 112      # stored as before...
    assert "336 total cases" in s.warning and "112" in s.warning    # ...but no longer silent


def test_unmapped_variety_and_blank_distributor_are_flagged():
    s = parse_production_text("CS Count SKU\n56 CS ZZTOP SPECIAL\n56 Total CasesNOWHERE.PO.1\n")
    assert any("not recognised" in w for w in s.warnings)
    assert any("distributor" in w for w in s.warnings)
    assert parse_production_text(CLEAN).warnings == []


def test_lot_and_date_glued_to_the_variety_are_not_part_of_its_name():
    s = parse_production_text("56 CS PARB-PLAIN 11500430264/30/2026\n"
                              "56 Total CasesOCALA.PO.054511751617\n")
    assert [(L.variety, L.cs_count) for L in s.lines] == [("Plain", 56)]


# --- store: warnings, issues, stub retry -------------------------------------

def _rec(text, mid, recv="2026-11-10T18:00:00Z", now_iso=None,
         subject="Daily Production OCALA.PO.054511751617"):
    sheet = parse_production_text(text, subject=subject, received_at=recv)
    return ps.record_from_sheet(sheet, message_id=mid, subject=subject, received_at=recv,
                                **({"now_iso": now_iso} if now_iso else {}))


def test_warning_is_stored_and_raises_a_feed_health_issue(datadir):
    bad = CLEAN.replace("112 Total", "336 Total")
    ps.store_parsed([(_rec(bad, "<w1>"), "", "")])
    [r] = it.load_production()
    assert "336" in r["warning"] and r["header_total_cases"] == 336 and r["line_total_cases"] == 112
    issues = feed_health.snapshot()["issues"]
    assert "production-sheet:<w1>" in issues and issues["production-sheet:<w1>"]["severity"] == "warn"
    # a clean re-read of the same email clears it
    ps.store_parsed([(_rec(CLEAN, "<w1>"), "", "")], refresh=True)
    assert "production-sheet:<w1>" not in feed_health.snapshot()["issues"]
    assert feed_health.snapshot()["feeds"]["production-webhook"]["ok"] is True


def test_unreadable_sheet_is_retried_on_three_days_before_it_is_seen(datadir):
    for i, day in enumerate(("2026-11-09", "2026-11-10", "2026-11-11"), start=1):
        st, _ = ps.store_parsed([(_rec("", "<s1>", now_iso=day + "T23:30:00"), "", "")])[0]
        assert st == "parse_error"
        recs = it.load_production()
        assert len(recs) == 1 and recs[0]["parse_attempts"] == i
        assert ("<s1>" in ps.seen_message_ids(recs)) is (i >= 3)
    # two attempts on the SAME day do not count twice
    ps.store_parsed([(_rec("", "<s2>", now_iso="2026-11-09T01:00:00"), "", "")])
    ps.store_parsed([(_rec("", "<s2>", now_iso="2026-11-09T23:30:00"), "", "")])
    assert "<s2>" not in ps.seen_message_ids(it.load_production())
    assert feed_health.snapshot()["issues"]["production-sheet:<s1>"]["severity"] == "error"


def test_stub_is_replaced_once_the_sheet_reads(datadir):
    ps.store_parsed([(_rec("", "<s1>"), "", "")])
    st, _ = ps.store_parsed([(_rec(CLEAN, "<s1>"), "", "")])[0]
    [r] = it.load_production()
    assert st == "ingested" and r["total_cases"] == 112 and not r["parse_error"]
    assert "production-sheet:<s1>" not in feed_health.snapshot()["issues"]


# --- summary + health --------------------------------------------------------

def _summary(monkeypatch, today, period="week"):
    import blueprints.production as bp
    import core.util as util
    from app import app
    monkeypatch.setattr(util, "business_today", lambda: today)
    with app.test_request_context(f"/api/production/summary?period={period}"):
        return bp.api_production_summary().get_json()


def test_this_week_is_really_this_week(datadir, monkeypatch):
    it.save_production([{"production_date": "2026-10-20", "warehouse": "Ocala, FL",
                         "distributor": "Cheney Brothers", "total_cases": 112,
                         "lines": [{"variety": "Plain", "cs_count": 112}],
                         "received_at": "2026-10-20T18:00:00Z", "source_message_id": "<a>"}])
    out = _summary(monkeypatch, date(2026, 11, 12))
    b = out["buckets"]
    assert b[0]["start"] == "2026-11-09" and b[0]["total_cs"] == 0 and b[0]["current"]
    assert [x["start"] for x in b] == ["2026-11-09", "2026-11-02", "2026-10-26", "2026-10-19"]
    assert b[-1]["total_cs"] == 112 and not b[-1].get("empty")
    h = out["health"]
    assert h["newest_production_date"] == "2026-10-20"
    assert h["business_days_since_last_sheet"] == 17 and h["stale"]
    assert "17 business days" in h["reasons"][0]


def test_recent_sheet_is_not_stale_and_incomplete_scan_is(datadir, monkeypatch):
    import blueprints.production as bp
    import core.util as util
    monkeypatch.setattr(util, "business_today", lambda: date(2026, 11, 12))
    it.save_production([{"production_date": "2026-11-10", "warehouse": "Ocala, FL",
                         "total_cases": 56, "lines": [{"variety": "Plain", "cs_count": 56}],
                         "received_at": "2026-11-10T18:00:00Z", "source_message_id": "<a>"},
                        {"parse_error": "no text", "lines": [], "source_subject": "Daily Production X",
                         "received_at": "2026-11-11T18:00:00Z", "source_message_id": "<b>",
                         "ingested_at": "2026-11-11T23:30:00"}])
    h = bp.production_health()
    assert h["business_days_since_last_sheet"] == 2 and not h["stale"]
    assert [a["kind"] for a in h["needs_attention"]] == ["unreadable"]
    assert h["needs_attention"][0]["will_retry"] is True
    feed_health.record("production-scan", False, "incomplete: jd@ aborted", complete=False)
    h = bp.production_health()
    assert h["stale"] and "did not finish" in h["reasons"][0]


# --- catch-up scan -----------------------------------------------------------

def _scan(monkeypatch, msgs, atts, texts, *, pages=None, body=None, folders_ok=True):
    import blueprints.production as bp
    import integrations.email_scanner as es
    from app import app
    monkeypatch.setattr(es.EmailInboxClient, "_ms365_token", lambda self: "tok")
    monkeypatch.setenv("MS365_USER", "jd@hhbagels.com")
    urls = []

    def page(url, tok):
        urls.append(url)
        return pages.pop(0) if pages else {"value": msgs}
    monkeypatch.setattr(bp, "_graph_page", page)

    def fake_get(self, url, token):
        if "/mailFolders/" in url:
            if not folders_ok:
                raise RuntimeError("HTTP 503")
            name = url.split("/mailFolders/")[1].split("?")[0]
            return json.dumps({"id": "F-" + name}).encode(), {}
        gid = url.split("/messages/")[1].split("/")[0]
        if url.endswith("/attachments"):
            return json.dumps({"value": atts[gid]}).encode(), {}
        aid = url.rsplit("/", 1)[1]
        return json.dumps({"contentBytes": base64.b64encode(aid.encode()).decode()}).encode(), {}
    monkeypatch.setattr(es.EmailInboxClient, "_graph_get", fake_get, raising=False)
    monkeypatch.setattr(bp, "parse_production_pdf", None, raising=False)
    import integrations.production_pdf_parser as pp
    monkeypatch.setattr(pp, "parse_production_pdf",
                        lambda b, subject="", received_at="": parse_production_text(
                            texts[b.decode()], subject=subject, received_at=received_at))
    with app.test_request_context(json=body or {"lookback_days": 14}):
        return bp.api_production_scan().get_json(), urls


def _msg(gid, mid, folder="F-inbox", subject="Daily Production OCALA.PO.054511751617"):
    return {"id": gid, "subject": subject, "parentFolderId": folder,
            "from": {"emailAddress": {"address": "gabo@hhbagels.com"}},
            "receivedDateTime": "2026-11-10T18:34:00Z", "internetMessageId": mid}


ZEB = ("CS Count SKU\n56 CS PARB-PLAIN\n56 Total CasesUS FOODS ZEBULON.PO.5210265G\n")


def test_scan_reads_every_pdf_and_every_folder_but_sent(datadir, monkeypatch):
    msgs = [_msg("g1", "<m1>", folder="F-rule-subfolder"), _msg("g2", "<m2>", folder="F-sentitems")]
    atts = {"g1": [{"id": "p1", "name": "ocala.pdf"}, {"id": "p2", "name": "zebulon.PDF"},
                   {"id": "x", "name": "notes.xlsx"}],
            "g2": [{"id": "p3", "name": "fwd.pdf"}]}
    out, urls = _scan(monkeypatch, msgs, atts, {"p1": CLEAN, "p2": ZEB, "p3": CLEAN})
    assert "/mailFolders/" not in urls[0]                    # all folders, not Inbox only
    recs = {r["source_message_id"]: r for r in it.load_production()}
    assert set(recs) == {"<m1>", "<m1>#2"}                   # first keeps the bare id; Sent skipped
    assert recs["<m1>#2"]["warehouse"] == "Zebulon, NC" and recs["<m1>"]["pdf_count"] == 2
    assert out["ingested"] == 2 and out["complete"] is True
    h = feed_health.snapshot()["feeds"]["production-scan"]
    assert h["ok"] and h["complete"] and h["ingested"] == 2
    assert h["newest_production_date"]
    # second run: nothing re-fetched, nothing duplicated
    out2, _ = _scan(monkeypatch, msgs, atts, {})
    assert out2["ingested"] == 0 and len(it.load_production()) == 2


def test_scan_picks_up_the_second_pdf_of_an_email_stored_before(datadir, monkeypatch):
    ps.store_parsed([(_rec(CLEAN, "<m1>"), "", "")])          # legacy: first PDF only
    atts = {"g1": [{"id": "p1", "name": "a.pdf"}, {"id": "p2", "name": "b.pdf"}]}
    out, _ = _scan(monkeypatch, [_msg("g1", "<m1>")], atts, {"p2": ZEB})
    assert out["ingested"] == 1
    assert {r["source_message_id"] for r in it.load_production()} == {"<m1>", "<m1>#2"}


def test_scan_that_stops_at_its_cap_is_not_complete(datadir, monkeypatch):
    pages = [{"value": [_msg("g1", "<m1>")], "@odata.nextLink": "https://next"}]
    out, _ = _scan(monkeypatch, [], {"g1": [{"id": "p1", "name": "a.pdf"}]}, {"p1": CLEAN},
                   pages=pages, body={"lookback_days": 14, "max_messages": 1})
    assert out["complete"] is False and out["truncated"] and out["ingested"] == 1
    h = feed_health.snapshot()["feeds"]["production-scan"]
    assert h["ok"] is False and h["complete"] is False and "more mail left" in h["detail"]


def test_scan_falls_back_to_inbox_when_folders_cannot_be_identified(datadir, monkeypatch):
    out, urls = _scan(monkeypatch, [_msg("g1", "<m1>")], {"g1": [{"id": "p1", "name": "a.pdf"}]},
                      {"p1": CLEAN}, folders_ok=False)
    assert "/mailFolders/Inbox/messages" in urls[0] and out["notes"] and out["ingested"] == 1
