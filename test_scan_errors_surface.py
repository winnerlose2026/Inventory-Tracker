"""Scan errors are reported, classified and persisted (review 2026-10-22).

Before: every scanner error collapsed to "N parse error(s) (see server log)",
status stayed "ok", scan_health only kept had_errors, and the cron printed
errors=0 because it read a key the report never had. A mailbox that stopped
answering, or a PO dropped whole for an unknown ship-to DC, was invisible.
"""
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import inventory_tracker as it
from integrations import email_scanner as ES

MAILBOX_ERR = f"{ES.MAILBOX_FAILURE_PREFIX}ms365 info@ms.hhbagels.com -- see server log"
PO_ERR = ("usfoods PO 9314683Y: ship-to DC 'TAMPA' did not resolve -- 3 line(s) "
          "skipped. Add it to usfoods_po_parser.USF_DC_CITY_TO_WAREHOUSE or check "
          "the ship-to block regex.")
ITEM_ERR = ("inventory & usage report (Zebulon, NC): unmapped MFG codes ['9999'] "
            "-- add them to hh_mfg_codes.HH_MFG_CODE_TO_VARIETY")


def _isolate(tmp: Path, monkeypatch):
    old = it.DATA_DIR
    monkeypatch.setattr(it, "DATA_DIR", tmp)
    for name in dir(it):
        v = getattr(it, name)
        # Earlier tests re-point it.DATA_DIR without restoring it, so match
        # the *_FILE constants by name as well as by directory.
        if isinstance(v, Path) and (v.parent == old or name.endswith("_FILE")):
            monkeypatch.setattr(it, name, tmp / v.name)
    it._write_json(tmp / "inventory.json", {})


def test_error_classes():
    assert ES.is_mailbox_failure(MAILBOX_ERR)
    assert not ES.is_mailbox_failure(PO_ERR)
    assert ES.is_po_failure(PO_ERR)
    assert ES.is_po_failure("cheney PO PDF parse failed ('x') -- see server log")
    assert ES.is_po_failure("chefs-warehouse PO ('x'): no PO# found in PDF -- skipped.")
    assert ES.is_po_failure("usfoods PO ('x'): 4 line(s) but no PO number found in "
                            "the PDF -- skipped. Check _PO_HEADER_RE")
    assert not ES.is_po_failure(ITEM_ERR)


def _scan_route(monkeypatch, tmp, errors):
    _isolate(tmp, monkeypatch)
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")

    def fake_scan(self, max_messages=300, filter_override=None):
        return ES.ScanResult(source="ms365", messages_seen=10, messages_parsed=0,
                             errors=list(errors))
    monkeypatch.setattr(ES.EmailInboxClient, "scan", fake_scan)
    monkeypatch.setattr(ES.EmailInboxClient, "source", lambda self: "ms365")
    from app import app
    c = app.test_client()
    r = c.post("/api/email/scan", json={"lookback_days": 14},
               headers={"X-Inventory-Token": "tok"})
    assert r.status_code == 200
    return c, r.get_json()["reports"][0]


def test_route_returns_and_persists_classified_errors(tmp_path, monkeypatch):
    c, rep = _scan_route(monkeypatch, tmp_path, [MAILBOX_ERR, PO_ERR, ITEM_ERR])
    assert rep["status"] == "partial"
    assert rep["errors"] == [MAILBOX_ERR, PO_ERR, ITEM_ERR]
    assert rep["mailbox_failures"] == [MAILBOX_ERR]
    assert rep["po_failures"] == [PO_ERR]
    assert rep["error"] == "scan completed with errors (see server log)"
    health = c.get("/api/scan/health", headers={"X-Inventory-Token": "tok"}).get_json()
    last = health["last_scan"]
    assert last["status"] == "partial" and last["had_errors"] is True
    assert last["errors"] == [MAILBOX_ERR, PO_ERR, ITEM_ERR]
    assert last["mailbox_failures"] == [MAILBOX_ERR] and last["po_failures"] == [PO_ERR]
    assert last["error_count"] == 3


def test_item_level_errors_keep_status_ok(tmp_path, monkeypatch):
    _c, rep = _scan_route(monkeypatch, tmp_path, [ITEM_ERR])
    assert rep["status"] == "ok"
    assert rep["errors"] == [ITEM_ERR] and rep["mailbox_failures"] == []


def test_mailbox_failure_text_carries_no_exception_detail(monkeypatch):
    """_scan_ms365 records the failure without the exception's text."""
    def boom(self, *a, **k):
        raise RuntimeError("Microsoft Graph GET /users/x failed (403). secret-ish")
    monkeypatch.setattr(ES.EmailInboxClient, "_scan_ms365_mailbox", boom)
    monkeypatch.setattr(ES.EmailInboxClient, "_ms365_token", lambda self: "t")
    monkeypatch.setenv("MS365_USER", "JD@ms.hhbagels.com,info@ms.hhbagels.com")
    res = ES.ScanResult(source="ms365")
    ES.EmailInboxClient()._scan_ms365(res, 10)
    assert len(res.errors) == 2 and all(ES.is_mailbox_failure(e) for e in res.errors)
    assert not any("403" in e or "secret" in e for e in res.errors)


# --- the cron's exit code ----------------------------------------------------

def _cron(monkeypatch, scan_report):
    sys.path.insert(0, str(HERE / "scripts"))
    import cowork_graph_scan
    G = importlib.reload(cowork_graph_scan)

    def post(url, token, payload, *, timeout):
        if url.endswith("/api/email/scan"):
            return 200, {"dry_run": False, "reports": [scan_report]}
        return 200, {"ok": True, "report": {"messages_seen": 1}}
    monkeypatch.setattr(G, "_post_json", post)
    args = SimpleNamespace(app_url="https://app", api_token="tok", lookback_hours=24,
                           lineage_lookback_hours=0, server_lookback_days=14,
                           server_max_messages=200, freight_lookback_days=14,
                           dry_run=False, screenshot_ocr=False)
    return G._run_server_mode(args)


def test_cron_fails_only_on_mailbox_failures(monkeypatch, capsys):
    base = {"messages_seen": 5, "messages_parsed": 1, "updated": 0, "unchanged": 1}
    assert _cron(monkeypatch, {**base, "status": "ok", "errors": [ITEM_ERR],
                               "mailbox_failures": [], "po_failures": []}) == 0
    out = capsys.readouterr().out
    assert "errors=1" in out and "errors: " + ITEM_ERR[:40] in out
    assert _cron(monkeypatch, {**base, "status": "partial", "errors": [PO_ERR],
                               "mailbox_failures": [], "po_failures": [PO_ERR]}) == 0
    assert "po_failures=1" in capsys.readouterr().out
    assert _cron(monkeypatch, {**base, "status": "partial", "errors": [MAILBOX_ERR],
                               "mailbox_failures": [MAILBOX_ERR],
                               "po_failures": []}) == 1
    assert _cron(monkeypatch, {**base, "status": "error", "errors": []}) == 1
