"""Shared fakes for the ingest-hardening tests: an isolated data dir, MIME
builders, and a fake Microsoft Graph mailbox for EmailInboxClient."""
import json
import urllib.parse
from email.message import EmailMessage
from pathlib import Path

import inventory_tracker as it
from integrations import email_scanner as ES


def isolate(tmp: Path, monkeypatch, inventory=None):
    """Point every data file at ``tmp`` (same approach as
    test_scan_errors_surface._isolate)."""
    old = it.DATA_DIR
    monkeypatch.setattr(it, "DATA_DIR", tmp)
    for name in dir(it):
        v = getattr(it, name)
        if isinstance(v, Path) and (v.parent == old or name.endswith("_FILE")):
            monkeypatch.setattr(it, name, tmp / v.name)
    it._FILE_CACHE.clear()
    it._write_json(tmp / "inventory.json", inventory or {})
    monkeypatch.chdir(tmp)   # blueprints/email.py locks ./data/.ingest.lock


def sku(variety, wh_short, warehouse, distributor="US Foods", qty=10.0, **extra):
    tag = {"US Foods": "USF", "Cheney Brothers": "CB"}[distributor]
    name = f"{variety} Bagel 4oz [{tag} - {wh_short}]"
    rec = {"name": name, "quantity": qty, "warehouse": warehouse,
           "distributor": distributor, "variety": variety, "unit": "cs",
           "case_size": 60, "price": 0.0, "case_cost": 27.0,
           "weekly_usage": 0.0, "on_order": [], "updated": "",
           "last_synced": "", "last_count_at": ""}
    rec.update(extra)
    return name.lower(), rec


def mime(sender, subject, *, text="", html="", attachments=(), date=None,
         msgid="<m1@x>", to="jd@hhbagels.com"):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = to
    m["Subject"] = subject
    m["Message-ID"] = msgid
    m["Date"] = date or "Mon, 09 Nov 2026 14:00:00 +0000"
    m.set_content(text or " ")
    if html:
        m.add_alternative(html, subtype="html")
    for fname, payload, (main, sub) in attachments:
        m.add_attachment(payload, maintype=main, subtype=sub, filename=fname)
    return m


PDF = ("application", "pdf")
XLSX = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
CSV = ("text", "csv")


class FakeGraph:
    """Stands in for EmailInboxClient._graph_get. ``messages`` is a list of
    dicts: {id, sender, subject, received, mime (EmailMessage), folder?, to?}."""

    def __init__(self, messages, page_size=50, fail=None):
        self.messages = list(messages)
        self.page_size = page_size
        self.fail = fail or {}        # url substring -> list of exceptions to raise
        self.calls = []

    def listing(self, m):
        return {
            "id": m["id"], "subject": m["subject"],
            "internetMessageId": m.get("internet_id") or f"<{m['id']}@x>",
            "from": {"emailAddress": {"address": m["sender"]}},
            "toRecipients": [{"emailAddress": {"address": a}}
                             for a in m.get("to", ["jd@hhbagels.com"])],
            "ccRecipients": [],
            "receivedDateTime": m.get("received", "2026-11-09T14:00:05Z"),
            "isRead": True, "hasAttachments": True,
            "parentFolderId": m.get("folder", "inbox-id"),
        }

    def __call__(self, url, token, accept="application/json"):
        self.calls.append(url)
        for frag, excs in self.fail.items():
            if frag in url and excs:
                raise excs.pop(0)
        path = urllib.parse.urlparse(url)
        if "/mailFolders/" in path.path and not path.path.endswith("/messages"):
            name = path.path.rsplit("/", 1)[1].lower()
            return json.dumps({"id": f"{name}-id"}).encode(), {}
        if path.path.endswith("/$value"):
            mid = path.path.split("/")[-2]
            m = next(x for x in self.messages if x["id"] == urllib.parse.unquote(mid))
            return m["mime"].as_bytes(), {}
        q = urllib.parse.parse_qs(path.query)
        skip = int((q.get("$skip") or ["0"])[0])
        rows = [self.listing(m) for m in self.messages]
        page = {"value": rows[skip:skip + self.page_size]}
        if skip + self.page_size < len(rows):
            page["@odata.nextLink"] = (url.split("&$skip=")[0]
                                       + f"&$skip={skip + self.page_size}")
        return json.dumps(page).encode(), {}


def scan_with(monkeypatch, graph, *, users="jd@ms.hhbagels.com", max_messages=50,
              filt="receivedDateTime ge 2026-10-29T00:00:00Z"):
    monkeypatch.setenv("MS365_USER", users)
    monkeypatch.setattr(ES.EmailInboxClient, "_ms365_token", lambda self: "t")
    monkeypatch.setattr(ES.EmailInboxClient, "_graph_get",
                        lambda self, url, token, accept="application/json":
                        graph(url, token, accept))
    res = ES.ScanResult(source="ms365")
    ES.EmailInboxClient()._scan_ms365(res, max_messages, filter_override=filt)
    return res


def route_scan(monkeypatch, tmp, result, body=None, inventory=None):
    """POST /api/email/scan with the scanner stubbed to return ``result``."""
    isolate(tmp, monkeypatch, inventory)
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    seen = {}

    def fake_scan(self, max_messages=300, filter_override=None):
        seen.update(max_messages=max_messages, filter_override=filter_override)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(ES.EmailInboxClient, "scan", fake_scan)
    monkeypatch.setattr(ES.EmailInboxClient, "source", lambda self: "ms365")
    from app import app
    c = app.test_client()
    r = c.post("/api/email/scan", json=body if body is not None else {"lookback_days": 14},
               headers={"X-Inventory-Token": "tok"})
    return c, r, seen
