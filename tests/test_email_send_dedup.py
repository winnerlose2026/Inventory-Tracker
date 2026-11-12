"""/api/email/send does not send the same message twice within 24 h
(review 2026-11-12, item 16)."""
import json
from datetime import datetime, timedelta

import inventory_tracker as it
from integrations import email_scanner as ES

from ingest_fakes import isolate

H = {"X-Inventory-Token": "tok"}
MSG = {"to": ["maria.hernandez@usfoods.com", "kathleen.thompson@usfoods.com"],
       "subject": "Weekly inventory report", "body": "Hi - could you send it?",
       "from": "jd@hhbagels.com"}


class _Resp:
    status = 202

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client(tmp_path, monkeypatch, fail=False):
    isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    monkeypatch.setenv("MS365_USER", "JD@ms.hhbagels.com")
    for k in ("MS365_SEND_AS", "EMAIL_SEND_ALLOWED_FROM", "EMAIL_SEND_ALLOWED_DOMAINS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ES.EmailInboxClient, "_ms365_token", lambda self: "t")
    sent = []

    def urlopen(req, timeout=30):
        if fail:
            raise OSError("network down")
        sent.append(json.loads(req.data.decode()))
        return _Resp()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    from app import app
    return app.test_client(), sent


def test_identical_send_is_a_duplicate_within_24h(tmp_path, monkeypatch):
    c, sent = _client(tmp_path, monkeypatch)
    d1 = c.post("/api/email/send", json=MSG, headers=H).get_json()
    assert d1["ok"] and d1["sent"] is True and d1["duplicate"] is False
    # Same message, recipients in another order.
    again = dict(MSG, to=list(reversed(MSG["to"])))
    r2 = c.post("/api/email/send", json=again, headers=H)
    d2 = r2.get_json()
    assert r2.status_code == 200
    assert d2 == {**d2, "ok": True, "sent": False, "duplicate": True}
    assert d2["first_sent_at"] and len(sent) == 1
    # A dry run says it would be a duplicate, and sends nothing.
    d3 = c.post("/api/email/send", json=dict(MSG, dry_run=True), headers=H).get_json()
    assert d3["duplicate"] is True and len(sent) == 1
    # A different body is a different message.
    d4 = c.post("/api/email/send", json=dict(MSG, body="Second ask."), headers=H).get_json()
    assert d4["sent"] is True and len(sent) == 2


def test_force_bypasses_and_ttl_expires(tmp_path, monkeypatch):
    c, sent = _client(tmp_path, monkeypatch)
    c.post("/api/email/send", json=MSG, headers=H)
    d = c.post("/api/email/send", json=dict(MSG, force=True), headers=H).get_json()
    assert d["sent"] is True and len(sent) == 2
    # Age the log entry past the TTL: the next identical send goes out.
    path = tmp_path / "sent_log.json"
    log = it._read_json(path, {})
    old = (datetime.now() - timedelta(hours=25)).isoformat()
    it._write_json(path, {k: {**v, "at": old} for k, v in log.items()})
    d = c.post("/api/email/send", json=MSG, headers=H).get_json()
    assert d["sent"] is True and len(sent) == 3


def test_idempotency_key_wins_over_content(tmp_path, monkeypatch):
    c, sent = _client(tmp_path, monkeypatch)
    a = dict(MSG, idempotency_key="chaser-2026-11-09-zebulon")
    assert c.post("/api/email/send", json=a, headers=H).get_json()["sent"] is True
    # Same key, different wording: still the same job -> not sent.
    b = dict(a, body="Reworded reminder")
    d = c.post("/api/email/send", json=b, headers=H).get_json()
    assert d["sent"] is False and d["duplicate"] is True
    # A new key with the same wording is a new job.
    c2 = dict(MSG, idempotency_key="chaser-2026-11-10-zebulon")
    assert c.post("/api/email/send", json=c2, headers=H).get_json()["sent"] is True
    assert len(sent) == 2


def test_failed_send_is_not_remembered(tmp_path, monkeypatch):
    c, _sent = _client(tmp_path, monkeypatch, fail=True)
    d = c.post("/api/email/send", json=MSG, headers=H).get_json()
    assert d["ok"] is False
    assert it._read_json(tmp_path / "sent_log.json", {}) == {}
    c, sent = _client(tmp_path, monkeypatch)
    d = c.post("/api/email/send", json=MSG, headers=H).get_json()
    assert d["sent"] is True and len(sent) == 1


def test_recipient_policy_is_unchanged(tmp_path, monkeypatch):
    c, sent = _client(tmp_path, monkeypatch)
    r = c.post("/api/email/send", json=dict(MSG, to="victim@gmail.com", force=True),
               headers=H)
    assert r.status_code == 400 and sent == []
