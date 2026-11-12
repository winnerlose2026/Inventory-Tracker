"""Graph subscription state is read and written safely, and a lost state is
recreated AND recorded (review 2026-11-12, item 17)."""
import pytest

import inventory_tracker as it
import integrations.graph_subscriptions as gs


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    monkeypatch.setattr(gs, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(gs, "_STATE_FILE", tmp_path / "graph_subscriptions.json")
    monkeypatch.setenv("GRAPH_SUB_CLIENT_STATE", "cs")
    monkeypatch.setenv("GRAPH_WEBHOOK_BASE", "https://example.onrender.com")
    monkeypatch.setenv("MS365_USER", "jd@hhbagels.com,info@hhbagels.com")
    monkeypatch.setattr(gs.EmailInboxClient, "_ms365_token", lambda self: "tok")
    it._FILE_CACHE.clear()
    yield tmp_path
    it._FILE_CACHE.clear()


def _graph(calls):
    def fake(method, url, token, body=None):
        calls.append((method, (body or {}).get("resource", "")))
        if method == "POST":
            return {"id": f"new-{len(calls)}",
                    "expirationDateTime": "2099-01-01T00:00:00Z"}
        return {"expirationDateTime": "2099-01-01T00:00:00Z"}
    return fake


def test_save_is_atomic_and_keeps_a_backup(env):
    gs._save_state({"subscriptions": [{"id": "a", "user": "jd@hhbagels.com"}]})
    gs._save_state({"subscriptions": [{"id": "b", "user": "jd@hhbagels.com"}]})
    assert [s["id"] for s in gs.list_subscriptions()] == ["b"]
    assert (env / "graph_subscriptions.json.bak").exists()
    assert not list(env.glob("graph_subscriptions.json.tmp.*"))


def test_torn_file_is_recovered_from_the_backup(env):
    gs._save_state({"subscriptions": [{"id": "a", "user": "jd@hhbagels.com"}]})
    gs._save_state({"subscriptions": [{"id": "b", "user": "jd@hhbagels.com"}]})
    (env / "graph_subscriptions.json").write_text('{"subscriptions": [{"id": ')
    it._FILE_CACHE.clear()
    assert [s["id"] for s in gs.list_subscriptions()] == ["a"]   # the .bak


def test_unreadable_state_is_reported_not_read_as_empty(env):
    (env / "graph_subscriptions.json").write_text("{not json")
    st = gs._load_state()
    assert st["subscriptions"] == [] and "unreadable" in st["state_error"]
    assert "unreadable" in gs.subscription_health()["state_error"]


def test_renew_recreates_and_records_when_state_is_lost(env, monkeypatch):
    from core import feed_health
    (env / "graph_subscriptions.json").write_text("{not json")
    calls = []
    monkeypatch.setattr(gs, "_graph_request", _graph(calls))
    out = gs.renew_subscriptions()
    assert out["ok"] is True and "unreadable" in out["state_error"]
    assert [m for m, _r in calls] == ["POST", "POST"]            # one per mailbox
    assert sorted(s["user"] for s in gs.list_subscriptions()) == [
        "info@hhbagels.com", "jd@hhbagels.com"]
    st = gs._load_state()
    assert "unreadable" in st["last_state_error"] and "state_error" not in st
    snap = feed_health.snapshot()
    assert "graph-subscriptions:state-lost" in snap["issues"]
    assert snap["feeds"]["graph-subscriptions"]["ok"] is True
    # Next day: the subscriptions exist, nothing to say.
    calls.clear()
    out = gs.renew_subscriptions()
    assert [m for m, _r in calls] == ["PATCH", "PATCH"] and "state_error" not in out


def test_renew_with_no_state_file_at_all_also_recreates_and_says_so(env, monkeypatch):
    calls = []
    monkeypatch.setattr(gs, "_graph_request", _graph(calls))
    out = gs.renew_subscriptions()
    assert len(calls) == 2 and "recreated" in out["state_error"]


def test_notification_stamp_does_not_bury_a_lost_state(env):
    (env / "graph_subscriptions.json").write_text("{not json")
    gs.record_notification_event()
    it._FILE_CACHE.clear()
    st = gs._load_state()
    assert st["last_notification_at"] and "unreadable" in st["last_state_error"]
