#!/usr/bin/env python3
"""One ingest path (production_store) for the scan, the webhook and /ingest.

Regressions covered:
  * Ocala 054511751617 was re-sent with the missing Cinnamon Raisin lot and
    stored twice (224 cs for a 112-cs PO) -- the newer sheet now replaces it.
  * Deleting a record didn't stick: the 14-day scan re-ingested it.
  * The scan's merge doubled every record without a message id (2 -> 4 -> 8).
  * /ingest without message_id now gets a stable content-hash id.
  * Lines read off the sheet claim lots before assorted-derived shares.
"""
import base64
import json
import sys
sys.path.insert(0, ".")

import pytest

import inventory_tracker as it
import integrations.production_store as ps
from integrations.production_pdf_parser import parse_production_text


@pytest.fixture
def datadir(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    monkeypatch.setattr(it, "PRODUCTION_FILE", tmp_path / "production.json")
    monkeypatch.setattr(ps, "ledger_po_index", lambda ledger=None: {})
    it._FILE_CACHE.clear()
    yield tmp_path
    it._FILE_CACHE.clear()


OCALA_FIRST = ("CS Count SKU\n8 CS PARB-POPPY\n24 CS PARB-ONION\n40 CS PARB-ASIAGO\n"
               "32 CS PARB-JALAPENO\n8 CS PARB-CINN-RAISIN\n"
               "112 Total CasesOCALA.PO.054511751617\nLot#\n"
               "1152091526\n1151091526\n1159091526\n1189091526\n")
OCALA_RESENT = OCALA_FIRST + "1155091526\n"


def _record(text, mid, recv, subject="Daily Production OCALA.PO.054511751617"):
    sheet = parse_production_text(text, subject=subject, received_at=recv)
    return ps.record_from_sheet(sheet, message_id=mid, subject=subject,
                                received_at=recv), sheet


def test_resent_sheet_replaces_the_first(datadir):
    r1, s1 = _record(OCALA_FIRST, "<m1>", "2026-09-16T18:34:00Z")
    r2, s2 = _record(OCALA_RESENT, "<m2>", "2026-09-16T18:40:52Z",
                     subject="Daily Production Sheet Template.xlsx")
    ps.store_parsed([(r1, s1.po_body, s1.po_subject)])
    [(status, stored)] = ps.store_parsed([(r2, s2.po_body, s2.po_subject)])
    recs = it.load_production()
    assert status == "replaced"
    assert len(recs) == 1 and recs[0]["total_cases"] == 112
    assert recs[0]["source_message_id"] == "<m2>" and recs[0]["replaced"] == ["<m1>"]
    # the older email arriving again is a duplicate, not a new row
    assert ps.store_parsed([(r1, "", "")])[0][0] == "duplicate"


def test_older_email_processed_after_newer_is_superseded(datadir):
    r1, _ = _record(OCALA_FIRST, "<m1>", "2026-09-16T18:34:00Z")
    r2, _ = _record(OCALA_RESENT, "<m2>", "2026-09-16T18:40:52Z")
    ps.store_parsed([(r2, "", "")])
    assert ps.store_parsed([(r1, "", "")])[0][0] == "superseded"
    recs = it.load_production()
    assert len(recs) == 1 and recs[0]["source_message_id"] == "<m2>"
    assert "<m1>" in ps.seen_message_ids(recs)


def test_a_different_bake_for_the_same_po_is_kept(datadir):
    r1, _ = _record(OCALA_FIRST, "<m1>", "2026-09-16T18:34:00Z")
    other = ("CS Count SKU\n56 CS PARB-PLAIN\n56 Total CasesOCALA.PO.054511751617\n"
             "Lot#\n1150091526\n")
    r2, _ = _record(other, "<m2>", "2026-09-16T19:00:00Z")
    ps.store_parsed([(r1, "", ""), (r2, "", "")])
    assert len(it.load_production()) == 2


def test_delete_leaves_a_tombstone_the_ingest_honours(datadir):
    import blueprints.production as bp
    from app import app
    r1, _ = _record(OCALA_FIRST, "<m1>", "2026-09-16T18:34:00Z")
    ps.store_parsed([(r1, "", "")])
    with app.test_request_context(method="DELETE"):
        out = bp.api_production_delete("<m1>").get_json()
    assert out["removed"] == 1 and it.load_production() == []
    assert "<m1>" in ps.load_tombstones()
    r1b, _ = _record(OCALA_FIRST, "<m1>", "2026-09-16T18:34:00Z")
    assert ps.store_parsed([(r1b, "", "")])[0][0] == "tombstoned"
    assert it.load_production() == []
    assert "<m1>" in ps.seen_message_ids([], ps.load_tombstones())


def test_scan_never_reappends_records_without_a_message_id(datadir, monkeypatch):
    import blueprints.production as bp
    import integrations.email_scanner as es
    from app import app
    it.save_production([
        {"po_number": "P1", "warehouse": "Ocala, FL", "production_date": "2026-10-01",
         "lines": [{"variety": "Plain", "cs_count": 56, "lot_number": "115010012026"}],
         "total_cases": 56, "source_message_id": ""},
    ])
    monkeypatch.setattr(es.EmailInboxClient, "_ms365_token", lambda self: "tok")
    monkeypatch.setenv("MS365_USER", "")
    for _ in range(3):
        with app.test_request_context(json={"lookback_days": 14}):
            bp.api_production_scan()
    assert len(it.load_production()) == 1


def test_scan_stores_through_the_shared_path(datadir, monkeypatch):
    """A scanned sheet keeps derived_from and a re-sent sheet replaces."""
    import blueprints.production as bp
    import integrations.email_scanner as es
    from app import app
    msgs = [
        {"id": "g1", "subject": "Daily Production OCALA.PO.054511751617",
         "from": {"emailAddress": {"address": "gabo@hhbagels.com"}},
         "receivedDateTime": "2026-09-16T18:34:00Z", "internetMessageId": "<m1>"},
        {"id": "g2", "subject": "Daily Production OCALA.PO.054511751617",
         "from": {"emailAddress": {"address": "gabo@hhbagels.com"}},
         "receivedDateTime": "2026-09-16T18:40:52Z", "internetMessageId": "<m2>"},
    ]
    texts = {"g1": OCALA_FIRST, "g2": OCALA_RESENT + "24 CS ASSORTED\n"}
    monkeypatch.setattr(es.EmailInboxClient, "_ms365_token", lambda self: "tok")
    monkeypatch.setenv("MS365_USER", "jd@hhbagels.com")
    monkeypatch.setattr(bp, "_graph_page", lambda url, tok: {"value": msgs})

    def fake_get(self, url, token):
        gid = url.split("/messages/")[1].split("/")[0]
        if url.endswith("/attachments"):
            return json.dumps({"value": [{"id": "a", "name": "s.pdf"}]}).encode(), {}
        return json.dumps({"contentBytes": base64.b64encode(gid.encode()).decode()}).encode(), {}
    monkeypatch.setattr(es.EmailInboxClient, "_graph_get", fake_get, raising=False)
    import integrations.production_pdf_parser as pp
    monkeypatch.setattr(pp, "_extract_text", lambda b: texts[b.decode()])
    with app.test_request_context(json={"lookback_days": 14}):
        out = bp.api_production_scan().get_json()
    recs = it.load_production()
    assert len(recs) == 1 and recs[0]["source_message_id"] == "<m2>"
    assert any(L.get("derived_from") == "ASSORTED" for L in recs[0]["lines"])
    assert out["ingested"] == 2


def test_ingest_without_message_id_is_idempotent(datadir, monkeypatch):
    import blueprints.production as bp
    import integrations.production_pdf_parser as pp
    from app import app
    monkeypatch.setattr(pp, "_extract_text", lambda b: OCALA_FIRST)
    body = {"pdf_b64": base64.b64encode(b"%PDF-same").decode(),
            "subject": "Daily Production OCALA.PO.054511751617",
            "received_at": "2026-09-16T18:34:00Z"}
    for expect in ("ingested", "duplicate"):
        with app.test_request_context(json=body):
            out = bp.api_production_ingest().get_json()
        assert out["status"] == expect
    recs = it.load_production()
    assert len(recs) == 1 and recs[0]["source_message_id"].startswith("sha256:")


def test_assorted_share_does_not_steal_the_real_lines_lot():
    text = ("CS Count SKU\n24 CS ASSORTED\n56 CS PARB-PLAIN\n80 Total CasesOCALA.PO.1\n"
            "Lot#\n115010012026\n")
    s = parse_production_text(text, received_at="2026-10-02T15:00:00Z")
    real = [L for L in s.lines if not L.derived_from]
    assert real[0].variety == "Plain" and real[0].lot_number == "115010012026"
    assert all(not L.lot_number for L in s.lines if L.derived_from)
