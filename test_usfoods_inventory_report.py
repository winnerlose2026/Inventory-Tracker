"""Tests for the US Foods "Weekly Bagel Inventory & Usage Report" body parser.

Covers the standalone parser (HTML + text fallback, nearest-week selection,
variety resolution via Vendor#/MFG and the USF-item fallback) and the
email_scanner integration (an inline-table message from a known rep yields
on_hand events with weekly_usage, keyed to the rep's warehouse).

Run:  python3 test_usfoods_inventory_report.py
"""

import email

from integrations import usfoods_inventory_report as R
from integrations.email_scanner import parse_message_with_errors


# Two weeks of forecast is enough to exercise nearest-week selection. The
# nearest week (earliest date, 5/31/2026) must win over 6/7/2026.
HEADER = ("ITEM", "Vendor#", "Description", "CURRENT ON HAND",
          "ON ORDER ETA 6/10", "Forecast 5/31/2026", "Forecast 6/7/2026")

ROWS = [
    # item,    vendor#, description,                     OH,  OO, wk1,    wk2
    ("1055010", "1184", "BAGEL, EGG 4.06 Z UNSL HEAT &",  23,  16, 5.39,  5.54),
    ("7095637", "1150", "BAGEL, PLN 4.25 Z UNSL PARBK",   81,  96, 22.28, 22.9),
    ("7309056", "1158", "BAGEL, EVTHG 4 Z UNSL PARBK",   101,  96, 24.8,  25.49),
    ("2954526", "1155", "BAGEL, CIN RAI 4.75 Z UNSL",     29,  40, 9.75,  10.02),
]

SENDER = "maria.hernandez@usfoods.com"


def _html(rows=ROWS, header=HEADER, include_quoted=True):
    def row_html(cells):
        return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
    table = "<table>" + row_html(header)
    for r in rows:
        table += row_html(r)
    table += "</table>"
    # A quoted older report (ITEM / CURRENT ON HAND only) sits below the
    # latest reply -- the parser must take the FIRST (top) table.
    quoted = ""
    if include_quoted:
        quoted = ("<table>"
                  "<tr><td>ITEM</td><td>CURRENT ON HAND</td><td>5/24/2026</td></tr>"
                  "<tr><td>1055010</td><td>25</td><td>5.14</td></tr>"
                  "</table>")
    return f"<html><body><p>Here is the information today.</p>{table}{quoted}</body></html>"


def _text(rows=ROWS, header=HEADER):
    """Outlook flattens the table to one cell per line, blank-separated."""
    cells = list(header)
    for r in rows:
        cells.extend(str(c) for c in r)
    return "Here is the information today.\n\n" + "\n\n".join(str(c) for c in cells) + "\n"


def _mime(html=None, text=None, sender=SENDER,
          subject="RE: Weekly Bagel Inventory & Usage Report — H&H Bagels"):
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f'"Hernandez, Maria" <{sender}>'
    msg["To"] = "JD Gross <JD@hhbagels.com>"
    msg["Message-ID"] = "<test-report@usfoods.com>"
    msg.set_content(text or "see html")
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return email.message_from_bytes(bytes(msg))


def test_sender_resolves_to_zebulon():
    dist, wh = R.warehouse_for_sender(f'"Hernandez, Maria" <{SENDER}>')
    assert dist == "US Foods", dist
    assert wh == "Zebulon, NC", wh
    # Unknown sender -> (None, None)
    assert R.warehouse_for_sender("someone@example.com") == (None, None)
    print("ok: sender resolves to Zebulon, NC")


def test_parse_html_nearest_week_and_varieties():
    rep = R.parse_report_html(_html(), distributor="US Foods", warehouse="Zebulon, NC")
    assert rep is not None
    assert rep.week_label == "Forecast 5/31/2026", rep.week_label
    assert rep.unmapped_codes == [], rep.unmapped_codes
    assert len(rep.lines) == len(ROWS), len(rep.lines)
    by_variety = {L.variety: L for L in rep.lines}
    assert by_variety["Plain"].cases_on_hand == 81
    assert by_variety["Plain"].weekly_usage == 22.28          # wk1, not wk2 (22.9)
    assert by_variety["Egg"].weekly_usage == 5.39
    assert by_variety["Everything"].cases_on_hand == 101
    assert by_variety["Plain"].usf_item_no == "7095637"
    print("ok: HTML parse picks nearest week + resolves all varieties")


def test_parse_html_ignores_quoted_older_report():
    # Top table has CURRENT ON HAND for Egg = 23; the quoted table says 25.
    rep = R.parse_report_html(_html(include_quoted=True))
    egg = next(L for L in rep.lines if L.variety == "Egg")
    assert egg.cases_on_hand == 23, egg.cases_on_hand
    print("ok: parses the latest (top) table, not the quoted one")


def test_text_fallback():
    rep = R.parse_report_text(_text(), distributor="US Foods", warehouse="Zebulon, NC")
    assert rep is not None
    assert len(rep.lines) == len(ROWS), len(rep.lines)
    by_variety = {L.variety: L for L in rep.lines}
    assert by_variety["Plain"].cases_on_hand == 81
    assert by_variety["Plain"].weekly_usage == 22.28
    print("ok: text fallback parses rows + nearest week")


def test_usf_item_fallback_without_vendor_column():
    """Original format (no Vendor# column) still resolves via USF item #."""
    header = ("ITEM", "CURRENT ON HAND", "5/31/2026", "6/7/2026")
    rows = [("7095637", 81, 22.28, 22.9), ("1055010", 23, 5.39, 5.54)]
    rep = R.parse_report_html(_html(rows=rows, header=header, include_quoted=False),
                              distributor="US Foods", warehouse="Zebulon, NC")
    assert rep is not None, "should still parse a Vendor#-less report"
    assert rep.unmapped_codes == [], rep.unmapped_codes
    varieties = {L.variety for L in rep.lines}
    assert {"Plain", "Egg"} <= varieties, varieties
    print("ok: USF item-number fallback resolves varieties without Vendor#")


def test_scanner_integration_emits_on_hand_events():
    msg = _mime(html=_html(), text=_text())
    events, errors, cw_pos = parse_message_with_errors(msg)
    assert not cw_pos
    assert errors == [], errors
    assert len(events) == len(ROWS), (len(events), errors)
    for e in events:
        assert e.event_type == "on_hand", e.event_type
        assert e.item.distributor == "US Foods"
        assert e.item.warehouse == "Zebulon, NC"
        assert e.item.unit == "cs"
        assert e.item.case_size == R.CASE_SIZE
        assert e.item.weekly_usage is not None
    plain = next(e for e in events if e.item.variety == "Plain")
    assert plain.item.quantity == 81
    assert plain.item.weekly_usage == 22.28
    assert plain.item.distributor_sku == "7095637"
    print("ok: scanner emits on_hand events w/ weekly_usage for a known rep")


def test_scanner_text_only_message():
    """Some clients send text/plain only -- the fallback must still fire."""
    msg = _mime(html=None, text=_text())
    events, errors, _ = parse_message_with_errors(msg)
    assert errors == [], errors
    assert len(events) == len(ROWS), (len(events), errors)
    print("ok: scanner handles a text-only report via the fallback")


def test_scanner_unknown_rep_surfaces_error():
    msg = _mime(html=_html(), sender="newrep@usfoods.com")
    events, errors, _ = parse_message_with_errors(msg)
    assert events == [], events
    assert any("unknown rep" in e for e in errors), errors
    print("ok: unknown rep surfaces an error instead of silently dropping")


def test_scanner_ignores_unrelated_mail():
    msg = _mime(html="<html><body><p>Lunch at noon?</p></body></html>",
                text="Lunch at noon?", subject="lunch")
    events, errors, _ = parse_message_with_errors(msg)
    assert events == [], events
    assert errors == [], errors
    print("ok: unrelated mail produces no events and no noise")


if __name__ == "__main__":
    test_sender_resolves_to_zebulon()
    test_parse_html_nearest_week_and_varieties()
    test_parse_html_ignores_quoted_older_report()
    test_text_fallback()
    test_usf_item_fallback_without_vendor_column()
    test_scanner_integration_emits_on_hand_events()
    test_scanner_text_only_message()
    test_scanner_unknown_rep_surfaces_error()
    test_scanner_ignores_unrelated_mail()
    print("\nAll usfoods_inventory_report tests passed.")
