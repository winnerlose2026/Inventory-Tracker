"""/api/email/send: the sender is pinned and recipients are allowlisted
(review 2026-10-22). The app-only Mail.Send token can send as ANY tenant
mailbox, and anyone with the API token or the shared login could pick the
'from' and mail any address with HTML."""
import sys

sys.path.insert(0, ".")

H = {"X-Inventory-Token": "tok"}


def _client(monkeypatch, **env):
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    monkeypatch.setenv("MS365_USER", "JD@ms.hhbagels.com,info@ms.hhbagels.com")
    for k in ("MS365_SEND_AS", "EMAIL_SEND_ALLOWED_FROM", "EMAIL_SEND_ALLOWED_DOMAINS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app import app
    return app.test_client()


def _send(c, **body):
    body.setdefault("subject", "s")
    body.setdefault("body", "b")
    body["dry_run"] = True
    return c.post("/api/email/send", json=body, headers=H)


def test_todays_chaser_call_still_works(monkeypatch):
    c = _client(monkeypatch)
    r = _send(c, to=["thomas.paxson@usfoods.com", "mross@cheneybrothers.com"],
              cc="jd@hhbagels.com", **{"from": "jd@hhbagels.com"})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"], d
    assert d["from"] == "jd@hhbagels.com" and d["from_overridden"] is False


def test_from_is_pinned(monkeypatch):
    c = _client(monkeypatch, MS365_SEND_AS="jd@hhbagels.com")
    d = _send(c, to="rep@usfoods.com", **{"from": "ceo@hhbagels.com"}).get_json()
    assert d["ok"] and d["from"] == "jd@hhbagels.com" and d["from_overridden"] is True
    # No MS365_SEND_AS: the default stays the first MS365_USER (old fallback).
    c = _client(monkeypatch)
    d = _send(c, to="rep@usfoods.com", **{"from": "jay@hhbagels.com"}).get_json()
    assert d["from"] == "JD@ms.hhbagels.com" and d["from_overridden"] is True
    # An explicitly allowed extra sender is honoured.
    c = _client(monkeypatch, EMAIL_SEND_ALLOWED_FROM="ops@hhbagels.com")
    d = _send(c, to="rep@usfoods.com", **{"from": "OPS@hhbagels.com"}).get_json()
    assert d["from"] == "OPS@hhbagels.com" and d["from_overridden"] is False


def test_recipients_outside_the_allowed_domains_are_refused(monkeypatch):
    c = _client(monkeypatch)
    r = _send(c, to=["rep@usfoods.com", "victim@gmail.com"])
    assert r.status_code == 400
    assert r.get_json()["refused"] == ["victim@gmail.com"]
    r = _send(c, to="rep@usfoods.com", cc=["x@usfoods.com.evil.example"])
    assert r.status_code == 400
    # Display-name / list syntax is not a plain address.
    assert _send(c, to='"Rep" <rep@usfoods.com>').status_code == 400
    assert _send(c, to="a@usfoods.com,b@evil.example").status_code == 400
    # Subdomains of an allowed domain are fine (JD@ms.hhbagels.com).
    assert _send(c, to="JD@ms.hhbagels.com").status_code == 200
    assert _send(c, to="bmorris@onelineage.com").status_code == 200


def test_allowed_domains_env_override(monkeypatch):
    c = _client(monkeypatch, EMAIL_SEND_ALLOWED_DOMAINS="example.org")
    assert _send(c, to="a@example.org").status_code == 200
    assert _send(c, to="rep@usfoods.com").status_code == 400


def test_delivery_token_still_cannot_send(monkeypatch):
    c = _client(monkeypatch, DELIVERY_API_TOKEN="dtok")
    r = c.post("/api/email/send", json={"to": "rep@usfoods.com", "dry_run": True},
               headers={"X-Inventory-Token": "dtok"})
    assert r.status_code == 401
