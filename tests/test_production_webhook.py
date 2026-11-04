#!/usr/bin/env python3
"""Real-time Daily Production ingest (Graph change notifications).

Nothing was ingested by the webhook after 2026-06-10. The handler fetched
the message, its attachments and the PDF and parsed it INSIDE the
response, for every inbound mail -- slow enough for Graph to throttle and
drop the endpoint -- and renew only recreated a subscription after a 404.
Now: 202 right after validation, processing in a background thread through
the shared production_store path (derived_from kept, same-sheet dedup),
recreate on any renew failure, and status on /api/scan/health.
"""
import sys
import threading
sys.path.insert(0, ".")

import pytest

import inventory_tracker as it
import integrations.graph_subscriptions as gs
import integrations.production_store as ps

CS = "secret-state"
TEXT = ("CS Count SKU\n24 CS ASSORTED\n56 CS PARB-PLAIN\n80 Total CasesOCALA.PO.054511000001\n"
        "Lot#\n115010012026\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    monkeypatch.setattr(it, "PRODUCTION_FILE", tmp_path / "production.json")
    monkeypatch.setattr(gs, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(gs, "_STATE_FILE", tmp_path / "graph_subscriptions.json")
    monkeypatch.setattr(ps, "ledger_po_index", lambda ledger=None: {})
    monkeypatch.setenv("GRAPH_SUB_CLIENT_STATE", CS)
    monkeypatch.setenv("GRAPH_WEBHOOK_BASE", "https://example.onrender.com")
    it._FILE_CACHE.clear()
    yield tmp_path
    it._FILE_CACHE.clear()


def _payload(cs=CS):
    return {"value": [{"clientState": cs,
                       "resource": "Users/u-1/Messages/g-1"}]}


def test_webhook_answers_202_before_processing(env, monkeypatch):
    import blueprints.webhooks as wh
    from app import app
    started = threading.Event()
    release = threading.Event()

    def slow(payload):
        started.set()
        release.wait(5)
        return {"ok": True}
    monkeypatch.setattr(gs, "handle_notification", slow)
    client = app.test_client()
    r = client.post("/webhooks/graph/notifications", json=_payload())
    assert r.status_code == 202 and r.get_json()["ok"] is True
    assert started.wait(2)          # work happens off the request thread
    release.set()
    bad = client.post("/webhooks/graph/notifications", json=_payload("wrong"))
    assert bad.status_code == 202 and bad.get_json()["ok"] is False


def test_notification_ingests_through_the_shared_path(env, monkeypatch):
    import integrations.production_pdf_parser as pp
    monkeypatch.setattr(gs.EmailInboxClient, "_ms365_token", lambda self: "tok")
    monkeypatch.setattr(gs, "_fetch_message", lambda u, m, t: {
        "subject": "Daily Production OCALA.PO.054511000001",
        "from": {"emailAddress": {"address": "gabo@hhbagels.com"}},
        "receivedDateTime": "2026-10-02T15:00:00Z", "hasAttachments": True,
        "internetMessageId": "<w1>"})
    monkeypatch.setattr(gs, "_fetch_first_pdf", lambda u, m, t: b"%PDF")
    monkeypatch.setattr(pp, "_extract_text", lambda b: TEXT)
    out = gs.handle_notification(_payload())
    assert out["ingested"] == 1
    recs = it.load_production()
    assert len(recs) == 1
    derived = [L for L in recs[0]["lines"] if L.get("derived_from")]
    assert len(derived) == 4                      # derived_from survives
    assert {L["cs_count"] for L in derived} == {6}
    # a second notification for the same message is a duplicate
    assert gs.handle_notification(_payload())["ingested"] == 0
    health = gs.subscription_health()
    assert health["last_notification_at"]


def test_renew_recreates_on_any_failure(env, monkeypatch):
    gs._save_state({"subscriptions": [{"id": "old", "user": "jd@hhbagels.com",
                                       "expiration_date_time": "2026-06-10T00:00:00Z"}]})
    monkeypatch.setenv("MS365_USER", "jd@hhbagels.com")
    monkeypatch.setattr(gs.EmailInboxClient, "_ms365_token", lambda self: "tok")
    calls = []

    def fake(method, url, token, body=None):
        calls.append(method)
        if method == "PATCH":
            raise RuntimeError("Graph PATCH failed: 403 Forbidden")
        if method == "POST":
            return {"id": "new", "expirationDateTime": "2099-01-01T00:00:00Z"}
        return {}
    monkeypatch.setattr(gs, "_graph_request", fake)
    out = gs.renew_subscriptions()
    assert out["ok"] is True
    assert calls == ["PATCH", "DELETE", "POST"]
    subs = gs.list_subscriptions()
    assert [s["id"] for s in subs] == ["new"]
    h = gs.subscription_health()
    assert h["active"] is True and h["subscriptions"][0]["id"] == "new"


def test_scan_health_reports_production_subscriptions(env, monkeypatch):
    from app import app
    gs._save_state({"subscriptions": [], "last_error": "token failed"})
    monkeypatch.setattr(it, "load_inventory", lambda: {"x": {"warehouse": "Ocala, FL"}})
    with app.test_request_context():
        import blueprints.email as em
        out = em.api_scan_health().get_json()
    assert out["production_subscriptions"]["active"] is False
    assert out["production_subscriptions"]["last_error"] == "token failed"


def test_graph_validation_handshake_with_colons_is_echoed():
    """Graph's real token contains colons. Rejecting them (2026-06-10 CodeQL
    hardening) 400'd every handshake, so Graph refused to renew or recreate
    the subscriptions and real-time production ingest stopped."""
    from urllib.parse import quote_plus
    from app import app
    tok = ("Validation: Testing client application reachability for "
           "subscription Request-Id: e7b03e20-6072-4dbc-a63d-a5f431647fe0")
    client = app.test_client()
    r = client.post("/webhooks/graph/notifications?validationToken=" + quote_plus(tok))
    assert r.status_code == 200
    assert r.get_data(as_text=True) == tok
    assert r.headers["Content-Type"].startswith("text/plain")
    bad = client.post("/webhooks/graph/notifications?validationToken="
                      + quote_plus("<script>alert(1)</script>"))
    assert bad.status_code == 400
