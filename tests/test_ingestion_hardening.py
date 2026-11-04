"""Mailbox-scan hardening (review 2026-10-22):

  * one unreadable attachment (ragged / cp1252 CSV) no longer discards every
    event in the email, and a "report" filename is not read as a PO;
  * an unparsed report recognised only by its attachment name survives the
    prune that runs in the same scan;
  * the sender is the From header's real address (or Graph's from address),
    never a display name, and "# distributor:" only counts from @hhbagels.com.
"""
import email
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

import inventory_tracker as it
from integrations import email_scanner as ES
from integrations import usfoods_inventory_report as R
from integrations import bagel_inventory_worksheet as W
from integrations.rep_map import sender_address
import test_usfoods_inventory_report as T


def _isolate(tmp: Path, monkeypatch):
    old = it.DATA_DIR
    monkeypatch.setattr(it, "DATA_DIR", tmp)
    for name in dir(it):
        v = getattr(it, name)
        # Earlier tests re-point it.DATA_DIR without restoring it, so match
        # the *_FILE constants by name as well as by directory.
        if isinstance(v, Path) and (v.parent == old or name.endswith("_FILE")):
            monkeypatch.setattr(it, name, tmp / v.name)


def _report_mail(sender='"Hernandez, Maria" <maria.hernandez@usfoods.com>',
                 attach=None, body_text="see html", html=True,
                 subject="Weekly Bagel Inventory & Usage Report - H&H Bagels - 10/19/26"):
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "info@ms.hhbagels.com"
    msg["Message-ID"] = "<hardening@usfoods.com>"
    msg["Date"] = "Mon, 19 Oct 2026 14:00:00 +0000"
    msg.set_content(body_text)
    if html:
        msg.add_alternative(T._html(include_quoted=False), subtype="html")
    for fname, data in (attach or []):
        msg.add_attachment(data, maintype="text", subtype="csv", filename=fname)
    return email.message_from_bytes(bytes(msg))


# --- D2: one bad attachment costs only itself --------------------------------

def test_ragged_csv_keeps_the_reports_events():
    m = _report_mail(attach=[("Inventory Detail.csv",
                              b"Item,Description,Cases\n7095637,BAGEL PLN,81,\n")])
    events, errors, _ = ES.parse_message_with_errors(m)
    assert len([e for e in events if e.event_type == "on_hand"]) == 4, events
    assert {e.item.warehouse for e in events} == {"Zebulon, NC"}


def test_cp1252_csv_keeps_the_reports_events_and_reads_the_csv():
    data = ("variety,warehouse,quantity\n"
            "Jalapeño Cheddar,Zebulon NC,7\n").encode("cp1252")
    m = _report_mail(attach=[("onhand.csv", data)])
    events, errors, _ = ES.parse_message_with_errors(m)
    csv_rows = [e for e in events if e.item.variety == "Jalapeño Cheddar"]
    assert len(csv_rows) == 1 and csv_rows[0].item.quantity == 7.0
    assert len(events) == 5


def test_a_crashing_part_records_an_error_and_the_rest_still_parses(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internals")
    monkeypatch.setattr(ES, "read_csv_bytes", boom)
    m = _report_mail(attach=[("x.csv", b"quantity\n1\n")])
    events, errors, _ = ES.parse_message_with_errors(m)
    assert len(events) == 4
    assert any("csv 'x.csv'" in e and "could not be read" in e for e in errors)
    assert not any("secret internals" in e for e in errors)   # no exception text


def test_report_filenames_are_not_purchase_orders():
    f = ES._infer_event_type_from_filename
    assert f("Inventory Report.csv") == "on_hand"     # "report" contains "po"
    assert f("Weekly Report.csv") == "on_hand"
    assert f("Export.csv") == "on_hand"
    assert f("PO_12345.csv") == "restock"
    assert f("USF PO 899412.csv") == "restock"
    assert f("WeeklyUsage.csv") == "usage"
    assert f("Invoices.csv") == "restock"
    assert f("OnHand.csv") == "on_hand"


# --- D3: filename-qualified unparsed entries survive the prune ----------------

def test_filename_qualified_entry_survives_same_scan_prune(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    entry = {"id": "<new-rep@usfoods.com>", "mailbox": "info@ms.hhbagels.com",
             "sender": "newrep@usfoods.com", "subject": "FW: numbers",
             "filenames": ["Customer Metric Source (7).xlsx"],
             "received": "2026-10-19T14:00:00Z", "sent": "2026-10-19T13:59:50Z"}
    it.record_unparsed_reports([entry])
    kept = it.prune_unparsed_reports(
        now=datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc), freshness=[])
    assert [e["id"] for e in kept] == ["<new-rep@usfoods.com>"]
    # The same entry WITHOUT its filenames (pre-fix shape) is still judged a
    # non-report, as before.
    bare = dict(entry, id="<bare>"); bare.pop("filenames")
    it.record_unparsed_reports([bare])
    kept = it.prune_unparsed_reports(
        now=datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc), freshness=[])
    assert [e["id"] for e in kept] == ["<new-rep@usfoods.com>"]


def test_scanner_stores_filenames_on_unparsed_entries(monkeypatch):
    """End to end through _scan_ms365_mailbox with a fake Graph: an unmapped
    USF sender, bland subject, an .xlsx we can't read -> queued WITH names."""
    msg = email.message.EmailMessage()
    msg["Subject"] = "FW: numbers"
    msg["From"] = "New Rep <newrep@usfoods.com>"
    msg["Date"] = "Mon, 19 Oct 2026 14:00:00 +0000"
    msg["Message-ID"] = "<fw-numbers@usfoods.com>"
    msg.set_content("attached")
    msg.add_attachment(b"not really a workbook", maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       filename="Customer Metric Source (7).xlsx")
    listing = {"value": [{"id": "g1", "subject": "FW: numbers",
                          "internetMessageId": "<fw-numbers@usfoods.com>",
                          "receivedDateTime": "2026-10-19T14:00:05Z",
                          "from": {"emailAddress": {"address": "newrep@usfoods.com"}}}]}

    def fake_get(self, url, token, accept="application/json"):
        if url.endswith("/$value"):
            return bytes(msg), {}
        return json.dumps(listing).encode(), {}
    monkeypatch.setattr(ES.EmailInboxClient, "_graph_get", fake_get)
    res = ES.ScanResult(source="ms365")
    ES.EmailInboxClient()._scan_ms365_mailbox(
        res, "tok", "info@ms.hhbagels.com", "Inbox",
        "receivedDateTime ge 2026-10-01T00:00:00Z", False, 10)
    assert len(res.unparsed) == 1, (res.unparsed, res.errors)
    assert res.unparsed[0]["filenames"] == ["Customer Metric Source (7).xlsx"]


# --- D5: the real address decides, never the display name --------------------

SPOOF = '"maria.hernandez@usfoods.com" <attacker@evil.example>'


def test_sender_address_ignores_display_names():
    assert sender_address(SPOOF) == "attacker@evil.example"
    assert sender_address('"Hernandez, Maria" <Maria.Hernandez@usfoods.com>') \
        == "maria.hernandez@usfoods.com"
    assert sender_address("tom.foley@usfoods.com") == "tom.foley@usfoods.com"
    # An unquoted display-name address is ambiguous: fail closed.
    assert sender_address("maria.hernandez@usfoods.com <attacker@evil.example>") \
        in ("", "attacker@evil.example")


def test_display_name_spoof_does_not_resolve_to_a_warehouse():
    assert R.warehouse_for_sender(SPOOF) == (None, None)
    assert W.warehouse_for_sender('"michael.via@usfoods.com" <x@evil.example>') \
        == (None, None)
    assert ES._distributor_from_sender('"po@usfoods.com" <x@evil.example>') is None
    # Real reps still resolve.
    assert R.warehouse_for_sender('"Hernandez, Maria" <maria.hernandez@usfoods.com>') \
        == ("US Foods", "Zebulon, NC")
    assert W.warehouse_for_sender("Michael Via <michael.via@usfoods.com>") \
        == ("US Foods", "Chicago, IL")


def test_spoofed_report_produces_no_events():
    m = _report_mail(sender=SPOOF)
    events, errors, _ = ES.parse_message_with_errors(m)
    assert [e for e in events if e.event_type == "on_hand"] == []


def test_graph_sender_is_authoritative_over_the_mime_header():
    # MIME From claims Maria, Graph says the mail came from elsewhere.
    m = _report_mail()
    events, _, _ = ES.parse_message_with_errors(m, sender_address="x@evil.example")
    assert events == []
    # And the other way round: Graph's real rep address wins over a mangled
    # MIME header.
    m = _report_mail(sender="garbled header")
    events, _, _ = ES.parse_message_with_errors(
        m, sender_address="maria.hernandez@usfoods.com")
    assert len(events) == 4 and {e.item.warehouse for e in events} == {"Zebulon, NC"}


def test_distributor_body_tag_only_from_hhbagels():
    body = "# distributor: US Foods\n# event: on_hand\nPlain @ Zebulon, NC: 12\n"
    ext = _report_mail(sender="Someone <someone@gmail.com>", body_text=body, html=False,
                       subject="numbers")
    ev, _, _ = ES.parse_message_with_errors(ext)
    assert ev and all(e.item.distributor == "Unassigned" for e in ev)
    internal = _report_mail(sender="JD <jd@hhbagels.com>", body_text=body, html=False,
                            subject="numbers")
    ev, _, _ = ES.parse_message_with_errors(internal)
    assert ev and all(e.item.distributor == "US Foods" for e in ev)
