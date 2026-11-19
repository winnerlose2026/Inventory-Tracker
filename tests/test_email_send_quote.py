"""quote_original keeps Outlook's quoted thread under an in-thread reply."""
from blueprints.email import _merge_reply_above_quote


def test_reply_goes_right_after_body_tag():
    draft = '<html><head></head><body dir="ltr"><div id="divRplyFwdMsg">From: Milaure</div><p>May I have the ETA</p></body></html>'
    out = _merge_reply_above_quote("<p>Ships 11/27</p>", "html", draft, "html")
    assert out.index("Ships 11/27") < out.index("divRplyFwdMsg") < out.index("May I have the ETA")
    assert out.startswith('<html><head></head><body dir="ltr"><div><p>Ships 11/27</p></div>')
    assert out.endswith("</body></html>")


def test_no_body_tag_prepends():
    out = _merge_reply_above_quote("<p>Hi</p>", "html", "<div>quoted</div>", "html")
    assert out.index("Hi") < out.index("quoted")


def test_plain_text_reply_is_escaped_with_line_breaks():
    out = _merge_reply_above_quote("a < b\nline2", "Text", "<body>q</body>", "html")
    assert "a &lt; b<br>line2" in out and out.index("line2") < out.index("q</body>")


def test_text_draft_is_wrapped():
    out = _merge_reply_above_quote("<p>Hi</p>", "html", "old & text", "text")
    assert "old &amp; text" in out


# --- endpoint: reply_to + quote_original --------------------------------------
import json

from integrations import email_scanner as ES
from ingest_fakes import isolate

H = {"X-Inventory-Token": "tok"}
DRAFT = {"id": "draft1", "body": {"contentType": "html",
         "content": '<html><body><div id="divRplyFwdMsg">From: Milaure</div>May I have the ETA</body></html>'}}


class _Resp:
    status = 200

    def __init__(self, data=b""):
        self._d = data

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    monkeypatch.setenv("MS365_USER", "JD@ms.hhbagels.com")
    for k in ("MS365_SEND_AS", "EMAIL_SEND_ALLOWED_FROM", "EMAIL_SEND_ALLOWED_DOMAINS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ES.EmailInboxClient, "_ms365_token", lambda self: "t")
    calls = []

    def urlopen(req, timeout=30):
        calls.append((req.get_method(), req.full_url,
                      json.loads(req.data.decode()) if req.data else None))
        if req.full_url.endswith("/createReply"):
            return _Resp(json.dumps(DRAFT).encode())
        return _Resp()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    from app import app
    return app.test_client(), calls


def _patch_body(calls):
    return [c for c in calls if c[0] == "PATCH"][0][2]["body"]


def test_quote_original_keeps_thread(tmp_path, monkeypatch):
    c, calls = _client(tmp_path, monkeypatch)
    d = c.post("/api/email/send", headers=H, json={
        "to": ["MFiltidor@chefswarehouse.com"], "cc": ["Isaiah@hhbagels.com"],
        "html": "<p>PO 1243276 ships 11/27</p>", "reply_to_message_id": "orig1",
        "quote_original": True}).get_json()
    assert d["ok"] and d["sent"] and d["threaded"]
    b = _patch_body(calls)
    assert b["contentType"] == "HTML"
    assert b["content"].index("ships 11/27") < b["content"].index("May I have the ETA")
    assert [c[0] for c in calls] == ["POST", "PATCH", "POST"]


def test_without_flag_body_is_replaced_as_before(tmp_path, monkeypatch):
    c, calls = _client(tmp_path, monkeypatch)
    c.post("/api/email/send", headers=H, json={
        "to": ["MFiltidor@chefswarehouse.com"], "html": "<p>only this</p>",
        "reply_to_message_id": "orig1"})
    b = _patch_body(calls)
    assert b == {"contentType": "HTML", "content": "<p>only this</p>"}
