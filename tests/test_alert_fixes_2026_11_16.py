"""The 11/16 "7 things need a look" banner and the La Mirada PO the tracker
never released. Each case below is a real message or file from that week.
"""
import io
import sys
from datetime import date, datetime, timedelta, timezone

import openpyxl

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from integrations import email_scanner as ES
from integrations import ingest_health as IH
from integrations import po_delivery as D
from integrations.cheney_inventory_report import (parse_report_xlsx,
                                                  warehouse_from_filename)


def _wb_multi(sheets):
    wb = openpyxl.Workbook()
    first = True
    for title, rows in sheets:
        ws = wb.active if first else wb.create_sheet()
        ws.title = title
        for r in rows:
            ws.append(r)
        first = False
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


USAGE = [
    ["Report Creation Date : 11/15/2026"],
    ["Drill Down Reporting : Date Range >= 11/09/2026 AND <= 11/14/2026"],
    ["Products", "Pack", "Dist Item #", "Mfq.Product Code", "GTIN", "Full Cases"],
    ["BAGEL PLAIN PARBAKED", "1:60 CT", "10153018", "1150", "", 40],
]
STOCK = [
    ["(60458211)H&H BAGELS ALTAMONTE SPRINGS"],
    ["08564 MICHAEL ROSS"],
    ["Item #", "Description", "Brand", "Pack", "Size", "UOM", "Stock"],
    ["FROZEN GROCERY"],
    ["10153018", "BAGEL PLAIN PARBAKED", "H & H", 1, "60CT", "cs", 34],
]


# --- Cheney weekly workbook ----------------------------------------------------

def test_a_misspelled_facility_still_maps():
    # 11/15: "HHBagelsRiviersBeachUsage-StockNov9-Nov142026.xlsx" was dropped whole
    for name in ("HHBagelsRiviersBeachUsage-StockNov9-Nov142026.xlsx",
                 "HHBagelsRiveraBeachUsage.xlsx", "HHBagelsRiveriaBeach.xlsx"):
        assert warehouse_from_filename(name) == "Riviera Beach, FL", name
    assert warehouse_from_filename("HHBagelsOcalaUsage-StockNov9-Nov142026.xlsx") == "Ocala, FL"
    assert warehouse_from_filename("HHBagelsPuntaGordaUsage-Stock.xlsx") == "Punta Gorda, FL"
    assert warehouse_from_filename("whatever.xlsx") == ""


def test_the_frozen_grocery_label_row_is_not_an_unmapped_item():
    b = _wb_multi([("Usage", USAGE), ("Stock", STOCK)])
    ev, err = parse_report_xlsx(b, "HHBagelsOcalaUsage-StockNov9-Nov142026.xlsx")
    assert {e["event_type"] for e in ev} == {"on_hand", "usage_rate"}
    assert not [e for e in err if "unmapped" in e], err


def test_a_real_unmapped_code_is_still_reported():
    stock = STOCK + [["10199999", "BAGEL MYSTERY", "H & H", 1, "60CT", "cs", 5]]
    _ev, err = parse_report_xlsx(_wb_multi([("Usage", USAGE), ("Stock", stock)]),
                                 "HHBagelsOcalaUsage.xlsx")
    assert any("10199999" in e for e in err), err


def test_a_partial_file_is_retired_by_a_later_count():
    # 11/9 14:20: Riviera file with usage only; 18:25: Ross resent it with stock
    only_usage = _wb_multi([("Usage", [r[:] for r in USAGE])])
    _ev, err = parse_report_xlsx(only_usage, "HHBagelsRivieraBeachUsage-StockNov2-Nov72026.xlsx")
    partial = [e for e in err if e.startswith("partial parse")]
    assert partial and "[period end 2026-11-14]" in partial[0], err

    class _It:
        warehouse = "Riviera Beach, FL"

    class _Ev:
        event_type = "on_hand"
        item = _It()
        count_date = "2026-11-15T15:40:42Z"
        source_received_at = ""

    assert ES.drop_superseded_partials(partial, [_Ev()]) == []
    # a count from BEFORE the period closed does not retire it
    _Ev.count_date = "2026-11-12T10:00:00Z"
    assert ES.drop_superseded_partials(partial, [_Ev()]) == partial
    # nor does one for another facility
    _Ev.count_date = "2026-11-16T10:00:00Z"
    _It.warehouse = "Ocala, FL"
    assert ES.drop_superseded_partials(partial, [_Ev()]) == partial


# --- PO cadence ------------------------------------------------------------------

def _days(*ago, today):
    return sorted(today - timedelta(days=a) for a in ago)


def test_silence_while_the_last_po_was_on_the_way_is_not_a_gap():
    # Hanover: 1220440 ordered 9/30 (47 days ago), delivered 11/10
    today = date(2026, 11, 16)
    days = _days(47, 70, 93, 116, 139, today=today)
    assert IH.po_gap_verdict(days, today)["overdue"]          # the old reading
    v = IH.po_gap_verdict(days, today, {"open": False, "last_arrival": date(2026, 11, 10)})
    assert not v["overdue"] and v["days_since_arrival"] == 6
    # an open PO alone does not excuse it (a PO stuck in Pending would hide gaps)
    assert IH.po_gap_verdict(days, today, {"open": True})["overdue"]
    # long after that delivery, silence counts again
    later = date(2027, 1, 20)
    assert IH.po_gap_verdict(days, later,
                             {"open": False, "last_arrival": date(2026, 11, 10)})["overdue"]


def test_receipts_by_warehouse_reads_the_ledger():
    ledger = [
        {"distributor": "Chefs Warehouse", "warehouse": "Hanover, MD", "status": "arrived",
         "ordered_at": "2026-09-30T00:00:00", "arrival_date": "2026-11-10T00:00:00"},
        {"distributor": "Chefs Warehouse", "warehouse": "Hanover, MD", "status": "arrived",
         "ordered_at": "2026-07-08T00:00:00", "arrival_date": "2026-08-07T00:00:00"},
        {"distributor": "Chefs Warehouse", "warehouse": "Opa Locka, FL", "status": "pending",
         "ordered_at": "2026-11-10T00:00:00"},
        {"distributor": "Chefs Warehouse", "warehouse": "Opa Locka, FL", "status": "canceled",
         "ordered_at": "2026-10-28T00:00:00"},
    ]
    r = IH.po_receipts_by_warehouse(ledger)
    assert r[("Chefs Warehouse", "Hanover, MD")] == {"open": False,
                                                     "last_arrival": date(2026, 11, 10)}
    assert r[("Chefs Warehouse", "Opa Locka, FL")]["open"] is True


# --- Lineage: the truck is at the dock -----------------------------------------------

ON_SITE_1113 = ('Per dispatch, "the driver is on site for waiting to unload."\n\n'
                "Belinda Morris\nLogistics Specialist, Senior")


def test_driver_on_site_is_an_arrival():
    got = D.confirmation_lines(ON_SITE_1113)
    assert [k for _l, k in got] == ["on_site"], got
    for text in ["The truck checked in at 0800.", "Driver is onsite now",
                 "The load is at the dock unloading"]:
        assert [k for _l, k in D.confirmation_lines(text)] == ["on_site"], text


def test_on_site_plans_and_problems_do_not_count():
    for text in ["The driver will be on site tomorrow at 0800.",
                 "Is the driver on site yet?",
                 "The driver is not on site yet.",
                 "Driver on site but the load was refused.",
                 "Once the driver is on site I will advise.",
                 "The driver is on site for the 11/20 appt"]:
        assert D.confirmation_lines(text) == [], text


def test_on_site_is_dated_to_when_it_was_sent_in_the_docks_zone():
    z = D.dock_zone("US Foods, 15155 Northam Street, La Mirada, CA 90638")
    assert D.on_site_at("2026-11-13T20:43:06Z", z) == ("2026-11-13T12:43:06-08:00", True)


def test_on_site_answer_to_a_one_po_chase_releases_it(monkeypatch):
    from test_delivery_server_confirm import CHASE_ONE, _scheduled, _sync
    from test_po_delivery import BELINDA
    out, store = _sync(monkeypatch, {"393072B2": _scheduled()},
                       [("site", 1, BELINDA, ON_SITE_1113)], CHASE_ONE)
    rec = store["tracking"]["393072B2"]
    assert rec["status"] == "delivered", out["confirmed_by_server"]
    assert rec["delivered_time_known"] is True
    assert rec["delivered_at"] == "2026-10-15T12:00:00-05:00"     # 17:00Z, Houston dock (CDT)
    assert rec["events"][-1]["evidence"]["quote"].startswith("Per dispatch")


def test_a_stated_delivery_time_outranks_the_on_site_relay(monkeypatch):
    # dispatch relays "on site" late; a stated time read in the same sync wins
    from test_delivery_server_confirm import CHASE_ONE, _scheduled, _sync
    from test_po_delivery import BELINDA
    out, store = _sync(monkeypatch, {"393072B2": _scheduled()},
                       [("site", 3, BELINDA, ON_SITE_1113),
                        ("dlv", 1, BELINDA, "This load delivered 0905AM/15th")], CHASE_ONE)
    rec = store["tracking"]["393072B2"]
    assert rec["status"] == "delivered"
    assert rec["delivered_at"] == "2026-10-15T09:05:00-05:00", rec["delivered_at"]


# --- scanned (image-only) production sheets ------------------------------------------

GABO_TEXT_1105 = ("Production Date\nCS Count SKU\n56 CS PLAIN\n13 CS SESAME\n"
                  "43 CS EVERYTHING\n112 Total CasesCWMID.PO.1220440\nLot#\n"
                  "1160110426\n1163110426\n1168110426\n11/4/2026\n")


def _blank_pdf():
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def _prod_client(tmp_path, monkeypatch):
    from ingest_fakes import isolate
    isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    from app import app
    return app.test_client()


def test_an_image_only_sheet_is_listed_then_read_from_ocr_text(tmp_path, monkeypatch):
    import base64
    import inventory_tracker as it
    c = _prod_client(tmp_path, monkeypatch)
    h = {"X-Inventory-Token": "tok"}
    pdf = base64.b64encode(_blank_pdf()).decode()
    recv = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"pdf_b64": pdf, "subject": "Daily Production CWMID.PO.1220440",
            "sender": "Gabo@hhbagels.com", "message_id": "<scan1>", "received_at": recv}
    r = c.post("/api/production/ingest", json=body, headers=h).get_json()
    assert r["status"] == "parse_error"
    lst = c.get("/api/production/unreadable?meta_only=1", headers=h).get_json()
    assert [s["message_id"] for s in lst["sheets"]] == ["<scan1>"], lst
    # the cron posts the same PDF back with the OCR'd text
    r = c.post("/api/production/ingest", json={**body, "ocr_text": GABO_TEXT_1105},
               headers=h).get_json()
    assert r["status"] == "ingested", r
    rec = r["record"]
    assert rec["ocr"] is True and "OCR" in rec["warning"]
    assert rec["po_number"] == "1220440" and sum(l["cs_count"] for l in rec["lines"]) == 112
    it._FILE_CACHE.clear()
    assert c.get("/api/production/unreadable?meta_only=1", headers=h).get_json()["sheets"] == []


def test_ocr_text_never_overrides_a_readable_pdf(tmp_path, monkeypatch):
    import base64
    from unittest import mock
    from integrations import production_pdf_parser as P
    c = _prod_client(tmp_path, monkeypatch)
    good = P.parse_production_text(GABO_TEXT_1105, subject="Daily Production CWMID.PO.1220440")
    with mock.patch("integrations.production_pdf_parser.parse_production_pdf",
                    return_value=good):
        r = c.post("/api/production/ingest", json={
            "pdf_b64": base64.b64encode(b"%PDF-1.4").decode(), "message_id": "<t>",
            "subject": "Daily Production CWMID.PO.1220440",
            "ocr_text": "999 CS PLAIN\n999 Total CasesCWMID.PO.1220440\n"},
            headers={"X-Inventory-Token": "tok"}).get_json()
    assert sum(l["cs_count"] for l in r["record"]["lines"]) == 112
    assert not r["record"].get("ocr")


# --- memory: the nightly out-of-memory restart (08:20 UTC, 11/14-11/16) -------------

def test_a_big_data_file_is_not_kept_in_the_parse_cache(tmp_path, monkeypatch):
    import inventory_tracker as it
    from ingest_fakes import isolate
    isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(it, "_CACHE_MAX_BYTES", 1000)
    rows = [{"guid": i, "item": "Plain Bagel"} for i in range(200)]   # > 1000 bytes
    it.save_sales(rows)
    assert str(it.SALES_FILE) not in it._FILE_CACHE
    got = it.load_sales()
    assert got == rows and str(it.SALES_FILE) not in it._FILE_CACHE
    got.append({"guid": "x"})                       # caller owns its copy
    assert len(it.load_sales()) == 200
    # small files are still cached
    it.save_inventory({})
    assert str(it.INVENTORY_FILE) in it._FILE_CACHE


def test_backup_streams_a_big_file_without_parsing_it(tmp_path, monkeypatch):
    import tarfile
    from integrations import backups as B
    monkeypatch.setattr(B, "SUMMARY_PARSE_MAX_BYTES", 100)
    big = tmp_path / "sales.json"
    big.write_text("[\n" + ",\n".join('{"a": %d}' % i for i in range(100)) + "\n]")
    s = B._summarize_path(big)
    assert s["parsed"] is False and s["bytes"] == big.stat().st_size and s["lines"] == 101
    small = tmp_path / "inventory.json"
    small.write_text("{}")
    assert B._summarize_path(small)["valid"] is True


# --- OCR rebuild of Gabo's scanned 11/5 Riviera Beach sheet (real OCR boxes) --------

def _ocr_boxes():
    import json
    from pathlib import Path
    p = Path(__file__).parent / "fixtures" / "production_ocr_riviera_1105.json"
    return [tuple(b) for b in json.loads(p.read_text())]


def test_ocr_boxes_rebuild_the_sheet_exactly():
    from integrations.production_ocr import sheet_text
    from integrations.production_pdf_parser import parse_production_text
    text, problems = sheet_text(_ocr_boxes())
    assert problems == []
    s = parse_production_text(text, subject="DAILY PRODUCTION RIVIERA BEACH PO.014511774519",
                              received_at="2026-11-05T15:44:22Z")
    assert (s.po_number, s.warehouse, s.production_date, s.total_cases) == \
        ("014511774519", "Riviera Beach, FL", "2026-11-03", 336)
    got = {(l.variety, l.cs_count, l.lot_number) for l in s.lines}
    assert got == {("Plain", 56, "1150110326"), ("Poppy Seed", 8, "1152110326"),
                   ("Onion", 8, "1151110326"), ("Sesame", 72, "1153110326"),
                   ("Whole Wheat", 32, "1156110326"), ("Asiago", 40, "1159110326"),
                   ("Blueberry", 24, "1171110326"), ("Jalapeno Cheddar", 40, "1189110326"),
                   ("Cinnamon Raisin", 56, "1155110326")}


def test_an_ocr_read_that_does_not_add_up_is_refused():
    from integrations.production_ocr import sheet_text
    boxes = _ocr_boxes()
    misread = [(x, y, h, "58 CS" if t == "56 CS" and y < 240 else t, s)
               for x, y, h, t, s in boxes]                    # 56 -> 58 on Plain
    _t, problems = sheet_text(misread)
    assert any("add up to 338" in p for p in problems), problems
    shaky = [(x, y, h, t, 0.5 if t == "8 CS" else s) for x, y, h, t, s in boxes]
    assert any("low confidence" in p for p in sheet_text(shaky)[1])
    no_lot = [b for b in boxes if b[3] != "1155110326"]
    assert any("lot number" in p for p in sheet_text(no_lot)[1])


def test_an_ocr_read_sheet_raises_no_alert_and_a_repost_clears_a_stale_one(tmp_path, monkeypatch):
    import base64
    from core import feed_health
    from integrations import production_store as ps
    c = _prod_client(tmp_path, monkeypatch)
    h = {"X-Inventory-Token": "tok"}
    body = {"pdf_b64": base64.b64encode(_blank_pdf()).decode(),
            "subject": "Daily Production CWMID.PO.1220440", "message_id": "<scan2>",
            "received_at": "2026-11-05T15:44:22Z", "ocr_text": GABO_TEXT_1105}
    assert c.post("/api/production/ingest", json=body, headers=h).get_json()["status"] == "ingested"
    key = ps.sheet_issue_key("<scan2>")
    assert key not in feed_health.snapshot()["issues"]
    feed_health.raise_issue(key, "stale", "warn")         # as left by the old rule
    assert c.post("/api/production/ingest", json=body, headers=h).get_json()["status"] == "duplicate"
    assert key not in feed_health.snapshot()["issues"]


# --- Manassas relabeled its on-hand column (Jasmin Gomez, 11/16 10:39 AM) ------------

def test_manassas_customer_metric_source_layout_parses():
    from integrations import usfoods_inventory_report as R
    rows = [["Customer Name (#)", "Product Description", "APN #", "Pack Size",
             "Mfg Code", "Cases", "Hand On Case "],
            ["HH BAGELS (91634139)", "BAGEL, EVTHG 4 Z UNSL PARBK", 7309056,
             "10/6/4.25 OZ", 1158, 12, 34],
            ["HH BAGELS (91634139)", "BAGEL, PLN 4.25 Z UNSL PARBK", 7095637,
             "10/6/4.25 OZ", 1150, 8, 34],
            ["HH BAGELS (91634139)", "BAGEL, ONION 4.25 Z UNSL PARBK", 7928199,
             "10/6/4.25 OZ", 1151, 1, 12]]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Customer Metric Source"
    for r in rows:
        ws.append(r)
    b = io.BytesIO()
    wb.save(b)
    rep = R.parse_report_xlsx(b.getvalue())
    assert rep is not None and rep.warnings == []
    got = {(l.cases_on_hand, l.weekly_usage) for l in rep.lines}
    assert got == {(34, 12), (34, 8), (12, 1)}, got
