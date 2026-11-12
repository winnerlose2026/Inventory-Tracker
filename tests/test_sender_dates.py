"""count_date / PO source_received_at no longer trust an implausible MIME Date
header, and a count dated in the future is refused (review 2026-11-12, item 5)."""
import sync_inventory as sync
from integrations import email_scanner as ES
from integrations.base import SyncItem
from integrations.usfoods_po_parser import UsFoodsPO, UsFoodsPOLine

from ingest_fakes import FakeGraph, PDF, mime, scan_with, sku

RECEIVED = "2026-11-09T14:00:05Z"
TABLE = ("<table><tr><td>ITEM</td><td>Vendor#</td><td>CURRENT ON HAND</td>"
         "<td>Forecast 11/15/2026</td></tr>"
         "<tr><td>7095637</td><td>1150</td><td>81</td><td>22</td></tr></table>")
REP = "maria.hernandez@usfoods.com"


def test_trusted_sent_time_window():
    f = ES._trusted_sent_time
    ok = "2026-11-09T13:59:50Z"
    assert f(ok, RECEIVED) == (ok, None)
    assert f(ok, "") == (ok, None)                      # nothing to check against
    assert f("", RECEIVED) == (RECEIVED, None)          # no Date header
    used, err = f("2026-12-25T00:00:00Z", RECEIVED)     # post-dated
    assert used == RECEIVED and "implausible" in err
    used, err = f("2026-10-20T00:00:00Z", RECEIVED)     # > 7 days before
    assert used == RECEIVED and err
    assert f("2026-11-04T00:00:00Z", RECEIVED)[1] is None   # 5 days: relay lag ok


def _report(date):
    return mime(REP, "Weekly numbers", html=TABLE, date=date)


def test_count_date_uses_receive_time_when_date_header_is_post_dated():
    events, errors, _ = ES.parse_message_with_errors(
        _report("Fri, 25 Dec 2026 09:00:00 +0000"), sender_address=REP,
        received_at=RECEIVED)
    assert [e.count_date for e in events] == [RECEIVED]
    assert any("implausible" in e for e in errors)
    # A sane header is kept as-is.
    events, errors, _ = ES.parse_message_with_errors(
        _report("Mon, 09 Nov 2026 13:59:50 +0000"), sender_address=REP,
        received_at=RECEIVED)
    assert events[0].count_date.startswith("2026-11-09T13:59:50") and not errors


def test_po_received_at_uses_receive_time_and_scan_passes_it(monkeypatch):
    po = UsFoodsPO(po_number="7135734C", po_revision="1", order_date="11/05/26",
                   warehouse="Zebulon, NC",
                   lines=[UsFoodsPOLine(usf_item_no="7095637", description="x",
                                        quantity=56.0, unit="CS", variety="Plain",
                                        list_cost=27.0, net_cost=27.0,
                                        scc_gtin="", pack="10/6/4.25",
                                        mfr_prod_no="1150", case_size=60)])
    monkeypatch.setattr(ES, "_usfoods_parse_po_pdf", lambda b: po)
    msg = mime("buyer@usfoods.com", "US Foods PO Request - 7135734C",
               date="Sat, 02 Jan 2027 00:00:00 +0000",
               attachments=[("PO.pdf", b"%PDF", PDF)])
    g = FakeGraph([{"id": "g1", "sender": "buyer@usfoods.com",
                    "subject": "US Foods PO Request - 7135734C", "mime": msg,
                    "received": RECEIVED}])
    res = scan_with(monkeypatch, g)
    assert [e.source_received_at for e in res.events] == [RECEIVED]
    assert any("implausible" in e for e in res.errors)


def test_future_dated_count_is_refused():
    key, rec = sku("Plain", "Zebulon", "Zebulon, NC", qty=40.0,
                   last_count_at="2026-11-02T14:00:00Z", last_count_qty=40.0)
    inv = {key: rec}
    for etype in ("on_hand", "usage_rate"):
        evt = ES.EmailEvent(
            event_type=etype,
            item=SyncItem(quantity=12.0, distributor="US Foods", variety="Plain",
                          warehouse="Zebulon, NC", unit="cs", weekly_usage=9.0),
            source_message_id="<m>", count_date="2026-11-20T00:00:00Z")
        report = {"unmatched": [], "changes": [], "updated": 0, "unchanged": 0}
        sync._apply_email_event(evt, inv, [], "2026-11-09T15:00:00", report, False)
        assert inv[key]["quantity"] == 40.0 and inv[key]["weekly_usage"] == 0.0
        assert "in the future" in report["rejected"][0] and report["issues"]
    # Up to a day ahead (time zones, clock skew) is fine.
    evt.event_type = "on_hand"
    evt.count_date = "2026-11-10T10:00:00Z"
    report = {"unmatched": [], "changes": [], "updated": 0, "unchanged": 0}
    sync._apply_email_event(evt, inv, [], "2026-11-09T15:00:00", report, False)
    assert inv[key]["quantity"] == 12.0
