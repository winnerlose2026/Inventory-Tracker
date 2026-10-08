"""One live ingest path, and the same document is never re-applied.

Until 2026-10-08 two scanners were live:
  * the Render cron ran scripts/cowork_graph_scan.py, which parsed mail itself
    and POSTed /api/email/ingest-events, stamping each document with Graph
    `receivedDateTime` (per-mailbox, trails the send by seconds to minutes);
  * the laptop Cowork task POSTed /api/email/scan, which stamps the MIME Date
    header (= Graph `sentDateTime`).
PO copies are ordered by that stamp, so each path read the other's booking of
the SAME email as an older document: reverse, re-book, operator dates lost.

Now: the cron script defaults to --mode server (it only asks the web service
to scan), local mode stamps sentDateTime, and the apply path never re-applies
a copy with the same lines and quantities sent within minutes of the booking.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

HERE = Path(__file__).parent
_TODAY = datetime.now(timezone.utc).date()


def _iso(days_ago: int, hhmmss: str = "16:00:00") -> str:
    return f"{(_TODAY - timedelta(days=days_ago)).isoformat()}T{hhmmss}Z"


# ---------------------------------------------------------------------------
# cron script: server mode
# ---------------------------------------------------------------------------

def _script():
    sys.path.insert(0, str(HERE / "scripts"))
    sys.path.insert(0, str(HERE))
    import cowork_graph_scan
    return importlib.reload(cowork_graph_scan)


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_EMAIL_OK = {"dry_run": False, "reports": [{
    "status": "ok", "messages_seen": 12, "messages_parsed": 5, "updated": 3,
    "unchanged": 40, "po_revisions_skipped": ["x"], "errors": [],
    "chefs_warehouse": {"added": 1, "updated": 0}}]}
_FREIGHT_OK = {"ok": True, "report": {"added": 0, "updated": 2, "errors": []}}


def _fake_server(calls, email_responses):
    email_responses = list(email_responses)

    def urlopen(req, timeout=None):
        url = req.full_url
        calls.append((url, json.loads(req.data.decode()),
                      req.get_header("X-inventory-token")))
        if "login.microsoftonline.com" in url or "graph.microsoft.com" in url:
            raise AssertionError("server mode must not talk to Graph")
        if url.endswith("/api/email/scan"):
            status, body = email_responses.pop(0)
            if status != 200:
                raise urllib.error.HTTPError(
                    url, status, "busy", {}, io.BytesIO(json.dumps(body).encode()))
            return _Resp(status, body)
        if url.endswith("/api/freight/scan"):
            return _Resp(200, _FREIGHT_OK)
        raise AssertionError(f"unexpected POST {url}")
    return urlopen


def _cron_env(monkeypatch):
    monkeypatch.setenv("APP_URL", "https://inv.example")
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok-xyz")
    monkeypatch.delenv("SCAN_MODE", raising=False)
    # Graph creds present, as on the cron: server mode must ignore them.
    monkeypatch.setenv("MS365_TENANT_ID", "t")
    monkeypatch.setenv("MS365_CLIENT_ID", "c")
    monkeypatch.setenv("MS365_CLIENT_SECRET", "s")


# The Render cron's startCommand, verbatim (dashboard, not render.yaml).
CRON_ARGS = ["--lookback-hours", "24", "--lineage-lookback-hours", "2160"]


def test_cron_command_asks_the_server_to_scan(monkeypatch, capsys):
    _cron_env(monkeypatch)
    cgs = _script()
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_server(calls, [(200, _EMAIL_OK)]))
    monkeypatch.setattr(cgs, "_graph_token", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("server mode must not fetch a Graph token")))
    rc = cgs.run(CRON_ARGS)
    assert rc == 0
    urls = [c[0] for c in calls]
    assert urls == ["https://inv.example/api/email/scan",
                    "https://inv.example/api/freight/scan"]
    assert all(c[2] == "tok-xyz" for c in calls)
    assert calls[0][1] == {"dry_run": False, "lookback_days": 14,
                           "max_messages": 200}
    assert calls[1][1]["lookback_days"] == 14
    out = capsys.readouterr()
    assert "email-scan: status=ok" in out.out
    assert "freight-scan:" in out.out
    assert "tok-xyz" not in out.out + out.err


def test_server_mode_waits_out_a_concurrent_scan(monkeypatch):
    _cron_env(monkeypatch)
    cgs = _script()
    import time
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    calls = []
    busy = {"ok": False, "error": "scan already running"}
    monkeypatch.setattr(urllib.request, "urlopen", _fake_server(
        calls, [(409, busy), (409, busy), (200, _EMAIL_OK)]))
    assert cgs.run(CRON_ARGS) == 0
    assert [c[0].rsplit("/", 1)[-1] for c in calls] == ["scan", "scan", "scan", "scan"]
    assert calls[-1][0].endswith("/api/freight/scan")
    assert slept == [90, 90]


def test_server_mode_reports_a_failed_scan(monkeypatch):
    _cron_env(monkeypatch)
    cgs = _script()
    calls = []
    bad = {"dry_run": False, "reports": [{"status": "error", "error": "boom"}]}
    monkeypatch.setattr(urllib.request, "urlopen",
                        _fake_server(calls, [(200, bad)]))
    assert cgs.run(CRON_ARGS) == 1
    # Freight still runs: one failure should not cost the other feed a slot.
    assert calls[-1][0].endswith("/api/freight/scan")


def test_server_mode_needs_url_and_token(monkeypatch):
    _cron_env(monkeypatch)
    monkeypatch.setenv("APP_URL", "")
    cgs = _script()
    assert cgs.run(CRON_ARGS) == 2


def test_scan_mode_env_selects_local(monkeypatch):
    _cron_env(monkeypatch)
    monkeypatch.setenv("SCAN_MODE", "local")
    cgs = _script()
    monkeypatch.setattr(cgs, "_run_server_mode",
                        lambda a: (_ for _ in ()).throw(AssertionError("server")))
    monkeypatch.setattr(cgs, "_graph_token", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("no graph in tests")))
    assert cgs.run(CRON_ARGS + ["--state", os.devnull]) == 1   # reached Graph


# ---------------------------------------------------------------------------
# cron script: local mode stamps the SENT time
# ---------------------------------------------------------------------------

def test_local_mode_stamps_sent_time_not_received_time(monkeypatch, tmp_path):
    _cron_env(monkeypatch)
    cgs = _script()
    sent, received = "2026-10-05T14:02:11Z", "2026-10-05T14:03:47Z"
    monkeypatch.setattr(cgs, "_graph_token", lambda *a, **k: "graph-tok")
    monkeypatch.setattr(cgs, "_list_recent_messages", lambda *a, **k: [{
        "id": "m1", "subject": "PO 4511767347",
        "from": {"emailAddress": {"address": "donotreply@cheneybrothers.com"}},
        "sentDateTime": sent, "receivedDateTime": received,
        "hasAttachments": True}])
    monkeypatch.setattr(cgs, "_list_message_attachments",
                        lambda *a, **k: [{"id": "a1", "name": "po.pdf",
                                          "contentType": "application/pdf"}])
    monkeypatch.setattr(cgs, "_fetch_attachment_bytes", lambda *a, **k: b"%PDF")
    seen = {}

    def fake_cheney(pdf, dist, mid, subject, *, received_at, sender):
        seen["received_at"] = received_at
        return [], []
    monkeypatch.setattr(cgs, "_cheney_po_to_events", fake_cheney)
    posted = []
    monkeypatch.setattr(cgs, "_post_ingest", lambda url, tok, payload, **k: (
        posted.append(payload) or (200, {"reports": [{"status": "ok"}]})))
    rc = cgs.run(CRON_ARGS + ["--mode", "local", "--state",
                              str(tmp_path / "seen.txt"),
                              "--mailboxes", "JD@ms.hhbagels.com"])
    assert rc == 0
    assert seen["received_at"] == sent


def test_local_mode_asks_graph_for_sent_time(monkeypatch):
    cgs = _script()
    got = {}

    def fake_get(token, path, **k):
        got["path"] = path
        return {"value": []}
    monkeypatch.setattr(cgs, "_graph_get", fake_get)
    cgs._list_recent_messages("t", "JD@ms.hhbagels.com",
                              datetime.now(timezone.utc))
    assert "sentDateTime" in got["path"]


# ---------------------------------------------------------------------------
# apply path: an identical copy is never re-applied
# ---------------------------------------------------------------------------

DIST, WH, SENDER, PO = ("Cheney Brothers", "Ocala, FL",
                        "donotreply@cheneybrothers.com", "054511799001")


def _setup(tmp: Path):
    os.environ["DATA_DIR"] = str(tmp)
    sys.path.insert(0, str(HERE))
    import inventory_tracker
    inventory_tracker.DATA_DIR = tmp
    inventory_tracker.INVENTORY_FILE = tmp / "inventory.json"
    inventory_tracker.USAGE_FILE = tmp / "usage.json"
    import sync_inventory
    sync_inventory.INVENTORY_FILE = inventory_tracker.INVENTORY_FILE
    from seed_bagels import BAGELS
    inventory_tracker.save_inventory(
        {b["name"].lower(): dict(b, on_order=[]) for b in BAGELS})
    inventory_tracker.save_usage([])
    return inventory_tracker, sync_inventory


def _doc(received, order_date, lines, msg_id="m1"):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    return [EmailEvent(
        event_type="restock",
        item=SyncItem(quantity=q, distributor=DIST, variety=v, warehouse=WH,
                      unit="cases"),
        source_message_id=msg_id, source_subject=PO, po_number=PO,
        po_revision="", po_order_date=order_date, source_received_at=received,
        source_sender=SENDER) for v, q in lines]


def _pending(it):
    out = {}
    for item in it.load_inventory().values():
        for p in (item.get("on_order") or []):
            if p.get("po_number") == PO:
                v = item["name"].split(" Bagel")[0]
                out[v] = out.get(v, 0.0) + float(p.get("qty") or 0)
    return out


def _onhand(it):
    return {i["name"].split(" Bagel")[0]: float(i.get("quantity") or 0)
            for i in it.load_inventory().values() if i.get("warehouse") == WH}


def _reversals(it):
    return [r for r in it.load_usage() if r.get("po_number") == PO
            and (r.get("reversal_of_revision") is not None
                 or r.get("superseded_by_revision"))]


LINES = [("Plain", 56), ("Everything", 24)]


def test_pending_po_restamped_by_the_other_path_is_left_alone():
    with TemporaryDirectory() as td:
        it, sync = _setup(Path(td))
        od = (_TODAY - timedelta(days=1)).isoformat()
        # Server path booked it with the Date header ...
        sync._apply_events(_doc(_iso(1, "14:02:11"), od, LINES), dry_run=False)
        inv = it.load_inventory()
        for item in inv.values():
            for p in item.get("on_order") or []:
                if p.get("po_number") == PO:
                    p["ship_date"] = (_TODAY + timedelta(days=3)).isoformat()
                    p["ship_date_source"] = "operator"
        it.save_inventory(inv)
        # ... the old cron path reads the same email at receivedDateTime,
        # 96 s later, which orders as NEWER.
        rep = sync._apply_events(_doc(_iso(1, "14:03:47"), od, LINES,
                                      msg_id="m1-info"), dry_run=False)
        assert rep.get("po_identical_skipped"), rep
        assert rep["po_revisions_superseded"] == [], rep
        assert _pending(it) == {"Plain": 56.0, "Everything": 24.0}
        assert _reversals(it) == []
        rows = [p for i in it.load_inventory().values()
                for p in (i.get("on_order") or []) if p.get("po_number") == PO]
        assert all(p.get("ship_date_source") == "operator" for p in rows)
        assert all(p.get("source_received_at") == _iso(1, "14:02:11") for p in rows)


def test_arrived_po_restamped_by_the_other_path_stays_arrived():
    with TemporaryDirectory() as td:
        it, sync = _setup(Path(td))
        od = (_TODAY - timedelta(days=45)).isoformat()
        sync._apply_events(_doc(_iso(45, "09:00:00"), od, LINES), dry_run=False)
        assert _pending(it) == {}
        before = _onhand(it)
        rep = sync._apply_events(_doc(_iso(45, "09:01:30"), od, LINES,
                                      msg_id="m1-info"), dry_run=False)
        assert rep.get("po_identical_skipped"), rep
        assert rep["po_revisions_superseded"] == [], rep
        assert _pending(it) == {}
        assert _onhand(it) == before
        assert _reversals(it) == []


def test_a_real_revision_minutes_later_still_supersedes():
    with TemporaryDirectory() as td:
        it, sync = _setup(Path(td))
        od = (_TODAY - timedelta(days=1)).isoformat()
        sync._apply_events(_doc(_iso(1, "14:02:11"), od, LINES), dry_run=False)
        rep = sync._apply_events(
            _doc(_iso(1, "14:20:00"), od, [("Plain", 40), ("Everything", 24)],
                 msg_id="m2"), dry_run=False)
        assert not rep.get("po_identical_skipped"), rep
        assert _pending(it) == {"Plain": 40.0, "Everything": 24.0}


def test_identical_reissue_weeks_later_reopens_an_arrived_po():
    """USF re-cuts an unshipped PO under the same number and content (Houston
    393072B2). That is a new email weeks later, not a second stamp of the
    first -- it must still re-open the booking."""
    with TemporaryDirectory() as td:
        it, sync = _setup(Path(td))
        od = (_TODAY - timedelta(days=49)).isoformat()
        sync._apply_events(_doc(_iso(49, "09:00:00"), od, LINES), dry_run=False)
        assert _pending(it) == {}
        rep = sync._apply_events(_doc(_iso(1, "10:00:00"), od, LINES,
                                      msg_id="m-recut"), dry_run=False)
        assert not rep.get("po_identical_skipped"), rep
        assert _pending(it) == {"Plain": 56.0, "Everything": 24.0}
