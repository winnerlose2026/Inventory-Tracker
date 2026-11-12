"""A reply that only QUOTES last week's report must not re-apply it
(review 2026-11-12, item 2)."""
from integrations import email_scanner as ES
from integrations import usfoods_inventory_report as R

from ingest_fakes import FakeGraph, mime, scan_with

SENDER = "maria.hernandez@usfoods.com"
SUBJECT = "RE: Weekly Bagel Inventory & Usage Report - H&H Bagels"
TABLE = ("<table><tr><td>ITEM</td><td>Vendor#</td><td>Description</td>"
         "<td>CURRENT ON HAND</td><td>Forecast 11/8/2026</td></tr>"
         "<tr><td>1055010</td><td>1184</td><td>BAGEL, EGG</td><td>23</td><td>5.4</td></tr>"
         "<tr><td>7095637</td><td>1150</td><td>BAGEL, PLN</td><td>81</td><td>22.3</td></tr>"
         "</table>")
OUTLOOK_REPLY = ("<html><body><p>Running late, will send tomorrow.</p>"
                 "<div id=\"divRplyFwdMsg\"><b>From:</b> JD</div>" + TABLE
                 + "</body></html>")
GMAIL_REPLY = ("<html><body><div>Running late, will send tomorrow.</div>"
               "<div class=\"gmail_quote\">On Mon, JD wrote:" + TABLE
               + "</div></body></html>")
TEXT_CELLS = "\n\n".join(["ITEM", "Vendor#", "Description", "CURRENT ON HAND",
                          "Forecast 11/8/2026",
                          "1055010", "1184", "BAGEL, EGG", "23", "5.4",
                          "7095637", "1150", "BAGEL, PLN", "81", "22.3"])
TEXT_REPLY = ("Running late, will send tomorrow.\n\n"
              "On Mon, Nov 2, 2026 at 9:14 AM JD Gross <jd@hhbagels.com> wrote:\n\n"
              + TEXT_CELLS + "\n")


def test_parser_reads_only_the_replys_own_part():
    assert R.parse_report_html(OUTLOOK_REPLY) is None
    assert R.parse_report_html(GMAIL_REPLY) is None
    assert R.parse_report_text(TEXT_REPLY) is None
    assert R.quoted_only_report(OUTLOOK_REPLY, "")
    assert R.quoted_only_report(GMAIL_REPLY, "")
    assert R.quoted_only_report("", TEXT_REPLY)


def test_fresh_table_above_the_quote_still_parses():
    html = ("<html><body><p>This week:</p>" + TABLE.replace(">23<", ">19<")
            + "<div id='divRplyFwdMsg'>old</div>" + TABLE + "</body></html>")
    rep = R.parse_report_html(html)
    assert [l.cases_on_hand for l in rep.lines] == [19.0, 81.0]
    assert not R.quoted_only_report(html, "")
    text = TEXT_CELLS + "\n\nFrom: JD Gross\n\n" + TEXT_CELLS
    assert len(R.parse_report_text(text).lines) == 2


def _events(html="", text=""):
    msg = mime(SENDER, SUBJECT, text=text, html=html)
    return ES.parse_message_with_errors(msg)


def test_quoted_only_reply_yields_no_events_and_a_held_error():
    for html, text in ((OUTLOOK_REPLY, ""), (GMAIL_REPLY, ""), ("", TEXT_REPLY)):
        events, errors, _cw = _events(html, text)
        assert events == [], (html[:40], text[:40])
        assert len(errors) == 1 and ES.is_report_held(errors[0])
        assert "Zebulon, NC" in errors[0] and "QUOTED" in errors[0]


def test_unmapped_sender_quoting_the_table_stays_quiet():
    """JD's own replies quote the table too -- not a held report."""
    msg = mime("jd@hhbagels.com", SUBJECT, html=OUTLOOK_REPLY, to=SENDER)
    events, errors, _cw = ES.parse_message_with_errors(msg)
    assert events == [] and errors == []


def test_scan_queues_the_reply_and_raises_report_held(monkeypatch):
    msg = mime(SENDER, SUBJECT, html=OUTLOOK_REPLY)
    g = FakeGraph([{"id": "g7", "sender": SENDER, "subject": SUBJECT, "mime": msg}])
    res = scan_with(monkeypatch, g)
    assert res.events == []
    assert [u["id"] for u in res.unparsed] == ["<g7@x>"]
    assert res.unparsed[0]["kind"] == "report"
    assert [i["key"] for i in res.issues] == ["report-held:Zebulon, NC:<g7@x>"]
