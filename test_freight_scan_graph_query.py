"""/api/freight/scan: the Graph list query and transient-error retry.

The 10/8 18:00 cron run failed because Graph answered 504 Gateway Timeout
to `$filter=hasAttachments eq true and receivedDateTime ge ...` over every
folder of JD@. The route now filters on receivedDateTime only (indexed, with
a matching $orderby), checks hasAttachments itself, and retries 429/5xx.
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

TOKEN = "test-token-123"
HERE = Path(__file__).parent


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()
        self.status = 200

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client(tmp: Path, monkeypatch):
    os.environ["DATA_DIR"] = str(tmp)
    monkeypatch.setenv("INVENTORY_API_TOKEN", TOKEN)
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("MS365_TENANT_ID", "t")
    monkeypatch.setenv("MS365_CLIENT_ID", "c")
    monkeypatch.setenv("MS365_CLIENT_SECRET", "s")
    monkeypatch.setenv("MS365_USER", "JD@ms.hhbagels.com")
    sys.path.insert(0, str(HERE))
    import inventory_tracker
    inventory_tracker.DATA_DIR = tmp
    inventory_tracker.INVENTORY_FILE = tmp / "inventory.json"
    inventory_tracker.USAGE_FILE = tmp / "usage.json"
    inventory_tracker.FREIGHT_INVOICES_FILE = tmp / "freight_invoices.json"
    from seed_bagels import BAGELS
    inventory_tracker.save_inventory(
        {b["name"].lower(): dict(b, on_order=[], unit="cs",
                                 low_stock_threshold=b.get("threshold", 0))
         for b in BAGELS})
    inventory_tracker.save_usage([])
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_list_query_is_indexed_and_504_is_retried(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    lists = []
    state = {"504s": 1}

    def urlopen(req, timeout=None):
        url = req.full_url
        if "login.microsoftonline.com" in url:
            return _Resp({"access_token": "graph-tok"})
        if "/messages?" in url:
            lists.append(url)
            if state["504s"]:
                state["504s"] -= 1
                raise urllib.error.HTTPError(url, 504, "Gateway Timeout", {},
                                             io.BytesIO(b"{}"))
            return _Resp({"value": [
                # Lineage sender but no attachment: skipped before any fetch.
                {"id": "x1", "subject": "Lineage Freight Billable Invoice",
                 "from": {"emailAddress": {"address": "noreply@tms.e2open.com"}},
                 "hasAttachments": False},
                {"id": "x2", "subject": "lunch",
                 "from": {"emailAddress": {"address": "a@b.com"}},
                 "hasAttachments": True},
            ]})
        raise AssertionError(f"unexpected Graph call {url}")

    with TemporaryDirectory() as td:
        c = _client(Path(td), monkeypatch)
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        r = c.post("/api/freight/scan", json={"lookback_days": 14, "dry_run": True},
                   headers={"X-Inventory-Token": TOKEN})
        body = r.get_json()
        assert r.status_code == 200 and body["ok"] is True, body
        assert len(lists) == 2                       # 504, then success
        q = urllib.parse.parse_qs(urllib.parse.urlparse(lists[-1]).query)
        assert "hasAttachments" not in q["$filter"][0]
        assert q["$filter"][0].startswith("receivedDateTime ge ")
        assert q["$orderby"] == ["receivedDateTime desc"]
        assert "hasAttachments" in q["$select"][0]


def test_persistent_graph_error_is_reported_not_raised(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = {"n": 0}

    def urlopen(req, timeout=None):
        url = req.full_url
        if "login.microsoftonline.com" in url:
            return _Resp({"access_token": "graph-tok"})
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 503, "Unavailable", {},
                                     io.BytesIO(b"{}"))

    with TemporaryDirectory() as td:
        c = _client(Path(td), monkeypatch)
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        r = c.post("/api/freight/scan", json={"lookback_days": 14, "dry_run": True},
                   headers={"X-Inventory-Token": TOKEN})
        assert r.status_code == 200
        assert r.get_json()["ok"] is False
        assert calls["n"] == 3                       # three tries, then give up
