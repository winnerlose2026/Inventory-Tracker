"""Graph GETs are retried; a message that still will not download is queued;
the cron retries a dead scan POST (review 2026-11-12, item 10)."""
import importlib
import io
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import inventory_tracker as it
from integrations import email_scanner as ES

from ingest_fakes import FakeGraph, mime, route_scan, scan_with

HERE = Path(__file__).resolve().parent.parent
REP = "maria.hernandez@usfoods.com"


class _Resp:
    headers = {}

    def __init__(self, body=b"{}"):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http(code, headers=None):
    return urllib.error.HTTPError("https://graph.microsoft.com/v1.0/x", code, "x",
                                  headers or {}, io.BytesIO(b"{}"))


def _client(monkeypatch, outcomes):
    sleeps, calls = [], []

    def urlopen(req, timeout=30):
        calls.append(req.full_url)
        o = outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o
    monkeypatch.setattr(ES.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(ES, "_sleep", sleeps.append)
    return ES.EmailInboxClient(), sleeps, calls


def test_retries_throttle_and_server_errors_then_succeeds(monkeypatch):
    c, sleeps, calls = _client(monkeypatch, [
        _http(429, {"Retry-After": "7"}), _http(503),
        urllib.error.URLError("reset"), _Resp(b"ok")])
    body, _ = c._graph_get("https://graph.microsoft.com/v1.0/x", "t")
    assert body == b"ok" and len(calls) == 4
    assert sleeps == [7.0, 2.0, 4.0]            # Retry-After honoured, then backoff


def test_gives_up_after_three_retries_and_does_not_retry_4xx(monkeypatch):
    c, sleeps, calls = _client(monkeypatch, [_http(503)] * 5)
    try:
        c._graph_get("https://graph.microsoft.com/v1.0/x", "t")
        raise AssertionError("should have raised")
    except RuntimeError as exc:
        assert "503" in str(exc)
    assert len(calls) == 4 and len(sleeps) == 3
    c, sleeps, calls = _client(monkeypatch, [_http(403), _Resp()])
    try:
        c._graph_get("https://graph.microsoft.com/v1.0/x", "t")
        raise AssertionError("should have raised")
    except RuntimeError:
        pass
    assert len(calls) == 1 and sleeps == []


def test_retry_budget_makes_a_long_outage_fail_fast(monkeypatch):
    monkeypatch.setattr(ES, "_GRAPH_RETRY_BUDGET_S", 5.0)
    c, sleeps, calls = _client(monkeypatch, [_http(503)] * 20)
    for _ in range(3):
        try:
            c._graph_get("https://graph.microsoft.com/v1.0/x", "t")
        except RuntimeError:
            pass
    assert sum(sleeps) <= 5.0 and len(calls) < 9


def test_message_that_will_not_download_is_queued_and_flagged(monkeypatch):
    msg = mime(REP, "Weekly Inventory & Usage Report", text="x")
    g = FakeGraph([{"id": "g1", "sender": REP,
                    "subject": "Weekly Inventory & Usage Report", "mime": msg}],
                  fail={"/$value": [RuntimeError("503 after retries")]})
    res = scan_with(monkeypatch, g)
    assert [(u["kind"], u["reason"], u["id"]) for u in res.unparsed] == [
        ("fetch", "fetch failed", "<g1@x>")]
    assert [i["key"] for i in res.issues] == ["fetch-failed:<g1@x>"]
    assert ES.failure_class(res.errors[0]) == "fetch-failed"


def test_fetch_entry_survives_the_report_prune_and_retires_when_fixed(tmp_path, monkeypatch):
    res = ES.ScanResult(source="ms365", messages_seen=1)
    res.unparsed.append({"kind": "fetch", "reason": "fetch failed", "id": "<g1@x>",
                         "mailbox": "jd@ms.hhbagels.com", "sender": REP,
                         "subject": "thanks", "filenames": [],
                         "received": "2026-11-09T14:00:05Z", "sent": ""})
    c, _r, _ = route_scan(monkeypatch, tmp_path, res)
    assert [u["id"] for u in it.load_unparsed_reports()] == ["<g1@x>"]
    res.unparsed.clear()      # next full scan downloads it fine
    c.post("/api/email/scan", json={"lookback_days": 14},
           headers={"X-Inventory-Token": "tok"})
    assert it.load_unparsed_reports() == []


def test_cron_retries_a_dead_scan_post_twice(monkeypatch):
    sys.path.insert(0, str(HERE / "scripts"))
    import cowork_graph_scan
    G = importlib.reload(cowork_graph_scan)
    rep = {"status": "ok", "messages_seen": 1, "messages_parsed": 0, "updated": 0,
           "unchanged": 0, "errors": [], "mailbox_failures": [], "po_failures": []}
    answers = [(502, "Bad Gateway"), (0, None), (200, {"reports": [rep]})]
    sleeps = []

    def post(url, token, payload, *, timeout):
        if url.endswith("/api/email/scan"):
            return answers.pop(0)
        return 200, {"ok": True, "report": {}}
    monkeypatch.setattr(G, "_post_json", post)
    monkeypatch.setattr("time.sleep", sleeps.append)
    args = SimpleNamespace(app_url="https://app", api_token="tok", lookback_hours=24,
                           lineage_lookback_hours=0, server_lookback_days=14,
                           server_max_messages=200, freight_lookback_days=14,
                           dry_run=False, screenshot_ocr=False)
    assert G._run_server_mode(args) == 0
    assert sleeps == [60, 60] and answers == []
    # Still dead after two retries: exit 1.
    answers[:] = [(503, "x")] * 3
    sleeps.clear()
    assert G._run_server_mode(args) == 1 and sleeps == [60, 60]
