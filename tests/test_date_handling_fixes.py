"""Week-column detection, USF order-date parsing and count-date comparison
(review 2026-11-12, item 15)."""
import sync_inventory as sync
from integrations import email_scanner as ES
from integrations import usfoods_inventory_report as R
from integrations.base import SyncItem

from ingest_fakes import sku


def test_only_forecast_or_bare_date_headers_are_usage_weeks():
    html = ("<table><tr><td>ITEM</td><td>Vendor#</td><td>CURRENT ON HAND</td>"
            "<td>PO due 11/20/2026</td><td>Updated 11/9/2026</td>"
            "<td>Forecast 11/15/2026</td><td>11/22/2026</td></tr>"
            "<tr><td>7095637</td><td>1150</td><td>81</td><td>500</td><td>900</td>"
            "<td>20</td><td>24</td></tr></table>")
    rep = R.parse_report_html(html)
    assert rep.lines[0].weekly_usage == 22.0        # (20 + 24) / 2, not 361
    assert rep.week_label == "Forecast 11/15/2026" and rep.warnings == []


def test_iso_from_usf_date_accepts_short_month_day_and_4_digit_year():
    f = ES._iso_from_usf_date
    assert f("06/05/26") == "2026-06-05"
    assert f("6/5/26") == "2026-06-05"
    assert f("11/5/2026") == "2026-11-05"
    assert f("13/40/26") == "" and f("") == "" and f("June 5") == ""


KEY, REC = sku("Plain", "Zebulon", "Zebulon, NC", qty=50.0)


def _evt(qty, count_date, rcv=""):
    return ES.EmailEvent(
        event_type="on_hand",
        item=SyncItem(quantity=qty, distributor="US Foods", variety="Plain",
                      warehouse="Zebulon, NC", unit="cs"),
        source_message_id="<m>", count_date=count_date, source_received_at=rcv)


def _apply(inv, evt):
    report = {"unmatched": [], "changes": [], "updated": 0, "unchanged": 0}
    sync._apply_email_event(evt, inv, [], "2026-11-09T20:00:00", report, False)
    return report


def test_count_dates_compare_as_instants_not_strings():
    # Stored with a local offset: 12:51 -04:00 == 16:51 UTC.
    inv = {KEY: dict(REC, last_count_at="2026-11-09T12:51:00-04:00",
                     last_count_qty=50.0)}
    # An EARLIER instant whose string sorts later ("…T14…Z" > "…T12…-04:00").
    rep = _apply(inv, _evt(10.0, "2026-11-09T14:00:00Z"))
    assert inv[KEY]["quantity"] == 50.0 and rep["unchanged"] == 1
    # The SAME instant spelled differently, same figure: a replay, not a change.
    rep = _apply(inv, _evt(50.0, "2026-11-09T16:51:00Z"))
    assert rep["unchanged"] == 1 and rep["changes"] == []
    assert inv[KEY]["last_count_at"] == "2026-11-09T12:51:00-04:00"
    # A later instant applies.
    _apply(inv, _evt(44.0, "2026-11-09T17:30:00+00:00"))
    assert inv[KEY]["quantity"] == 44.0


def test_stamp_cmp_falls_back_to_strings_when_unparseable():
    assert sync._stamp_cmp("2026-11-09T16:51:00Z", "2026-11-09T12:51:00-04:00") == 0
    assert sync._stamp_cmp("2026-11-08", "2026-11-09T00:00:00Z") == -1
    assert sync._stamp_cmp("week 45", "week 44") == 1
