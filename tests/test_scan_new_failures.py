"""Failures no longer exit 0: /api/email/scan reports each problem once as a
new failure, the cron fails on it, a failed scan still stamps scan health, and
/api/scan/health is not ok when a mailbox could not be read
(review 2026-11-12, item 7)."""
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

from integrations import email_scanner as ES
from integrations.base import SyncItem

from ingest_fakes import route_scan

HERE = Path(__file__).resolve().parent.parent
H = {"X-Inventory-Token": "tok"}
UNMAPPED = ("usfoods PO 7135734C: unmapped USF item #s ['9999999'] — add them "
            "to usfoods_po_parser.USF_ITEM_TO_VARIETY")
UNKNOWN_REP = ("inventory & usage report ('Weekly'): unknown rep 'new@usfoods.com' "
               "-- add them to usfoods_inventory_report.REPORT_SENDER_TO_WAREHOUSE")
INFO = ("count date 2026-10-01 from the subject is 39 day(s) from the send date "
        "2026-11-09; using the send date.")
MAILBOX = f"{ES.MAILBOX_FAILURE_PREFIX}ms365 info@ms.hhbagels.com -- see server log"


def _result(errors=(), unparsed=(), events=()):
    res = ES.ScanResult(source="ms365", messages_seen=3, errors=list(errors),
                        events=list(events))
    res.unparsed.extend(unparsed)
    return res


def test_failure_classes():
    assert ES.failure_class(UNMAPPED) == "unmapped"
    assert ES.failure_class(UNKNOWN_REP) == "unknown-rep"
    assert ES.failure_class("PO PDF 'x': could not be read -- 0 lines") == "po-failure"
    assert ES.failure_class("xlsx 'a.xlsx': could not be read (see server log)") == "unreadable"
    assert ES.failure_class(INFO) is None
    assert ES.failure_class(MAILBOX) is None      # fails every run on its own


def test_each_problem_is_a_new_failure_exactly_once(tmp_path, monkeypatch):
    unparsed = [{"kind": "report", "reason": "no events parsed", "id": "<u1@x>",
                 "mailbox": "jd@ms.hhbagels.com", "sender": "maria.hernandez@usfoods.com",
                 "subject": "Weekly Inventory & Usage Report", "filenames": [],
                 "received": "2026-11-09T14:00:05Z", "sent": ""}]
    evt = ES.EmailEvent(event_type="on_hand",
                        item=SyncItem(quantity=5, distributor="US Foods",
                                      variety="Rainbow", warehouse="Nowhere, ZZ"))
    res = _result([UNMAPPED, UNKNOWN_REP, INFO], unparsed, [evt])
    c, r, _ = route_scan(monkeypatch, tmp_path, res)
    body = r.get_json()
    keys = sorted(f["key"].split(":")[0] for f in body["new_failures"])
    assert keys == ["unknown-rep", "unmapped", "unmatched", "unparsed"]
    assert body["reports"][0]["new_failures"] == body["new_failures"]
    assert body["reports"][0]["status"] == "ok"
    # Same scan again: nothing is new.
    r2 = c.post("/api/email/scan", json={"lookback_days": 14}, headers=H)
    assert r2.get_json()["new_failures"] == []


def test_dry_run_reports_but_does_not_remember(tmp_path, monkeypatch):
    c, r, _ = route_scan(monkeypatch, tmp_path, _result([UNMAPPED]),
                         body={"dry_run": True, "lookback_days": 14})
    assert len(r.get_json()["new_failures"]) == 1
    r2 = c.post("/api/email/scan", json={"lookback_days": 14}, headers=H)
    assert len(r2.get_json()["new_failures"]) == 1     # the real run is the first


def test_resolved_issue_is_cleared_by_a_full_clean_scan(tmp_path, monkeypatch):
    from core import feed_health
    res = _result([UNMAPPED])
    c, _r, _ = route_scan(monkeypatch, tmp_path, res)
    assert any(k.startswith("unmapped:") for k in feed_health.snapshot()["issues"])
    res.errors.clear()
    # A narrow window could not have re-seen it: kept.
    c.post("/api/email/scan", json={"lookback_days": 2}, headers=H)
    assert any(k.startswith("unmapped:") for k in feed_health.snapshot()["issues"])
    c.post("/api/email/scan", json={"lookback_days": 14}, headers=H)
    assert not any(k.startswith("unmapped:") for k in feed_health.snapshot()["issues"])


def test_failed_scan_still_stamps_health_and_health_is_not_ok(tmp_path, monkeypatch):
    from core import feed_health
    c, r, _ = route_scan(monkeypatch, tmp_path, _result())
    good_ts = c.get("/api/scan/health", headers=H).get_json()["last_scan"]["ts"]
    assert c.get("/api/scan/health", headers=H).get_json()["scan_ok"] is True
    assert feed_health.snapshot()["feeds"]["mailbox-scan"]["ok"] is True

    def boom(self, max_messages=300, filter_override=None):
        raise RuntimeError("Graph token endpoint down")
    monkeypatch.setattr(ES.EmailInboxClient, "scan", boom)
    r = c.post("/api/email/scan", json={"lookback_days": 14}, headers=H)
    assert r.get_json()["reports"][0]["status"] == "error"
    h = c.get("/api/scan/health", headers=H).get_json()
    assert h["last_scan"]["ts"] == good_ts                 # last GOOD scan kept
    assert h["last_attempt"]["status"] == "error" and h["last_attempt"]["ok"] is False
    assert h["ok"] is False and h["scan_ok"] is False
    feed = feed_health.snapshot()["feeds"]["mailbox-scan"]
    assert feed["ok"] is False and feed["last_ok"] and feed["trigger"] == "api"


def test_health_not_ok_when_latest_attempt_had_mailbox_failures(tmp_path, monkeypatch):
    c, r, _ = route_scan(monkeypatch, tmp_path, _result([MAILBOX]),
                         inventory={"x": {"name": "x", "quantity": 1}})
    assert r.get_json()["reports"][0]["mailbox_failures"] == [MAILBOX]
    h = c.get("/api/scan/health", headers=H).get_json()
    assert h["ok"] is False and h["last_attempt"]["mailbox_failures"] == [MAILBOX]


# --- the cron's exit code ---------------------------------------------------

def _cron(monkeypatch, body, dry_run=False):
    sys.path.insert(0, str(HERE / "scripts"))
    import cowork_graph_scan
    G = importlib.reload(cowork_graph_scan)

    def post(url, token, payload, *, timeout):
        if url.endswith("/api/email/scan"):
            return 200, body
        return 200, {"ok": True, "report": {"messages_seen": 1}}
    monkeypatch.setattr(G, "_post_json", post)
    args = SimpleNamespace(app_url="https://app", api_token="tok", lookback_hours=24,
                           lineage_lookback_hours=0, server_lookback_days=14,
                           server_max_messages=200, freight_lookback_days=14,
                           dry_run=dry_run, screenshot_ocr=False)
    return G._run_server_mode(args)


def test_cron_exits_1_only_when_a_failure_is_new(monkeypatch, capsys):
    rep = {"status": "ok", "messages_seen": 5, "messages_parsed": 1, "updated": 0,
           "unchanged": 1, "errors": [UNMAPPED], "mailbox_failures": [],
           "po_failures": []}
    new = [{"key": "unmapped:abc", "message": UNMAPPED, "severity": "warn"}]
    assert _cron(monkeypatch, {"reports": [rep], "new_failures": new}) == 1
    assert "NEW FAILURES: 1" in capsys.readouterr().err
    # The same error on the next run is no longer new: exit 0.
    assert _cron(monkeypatch, {"reports": [rep], "new_failures": []}) == 0
