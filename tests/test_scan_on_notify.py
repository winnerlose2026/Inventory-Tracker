"""A Graph notification for distributor / rep mail triggers a debounced
mailbox scan (feature B, 2026-11-12)."""
import pytest

import blueprints.webhooks as wh
import inventory_tracker as it
import integrations.graph_subscriptions as gs
import integrations.production_store as ps

from ingest_fakes import isolate

CS = "secret-state"


@pytest.fixture
def env(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(gs, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(gs, "_STATE_FILE", tmp_path / "graph_subscriptions.json")
    monkeypatch.setattr(ps, "ledger_po_index", lambda ledger=None: {})
    monkeypatch.setenv("GRAPH_SUB_CLIENT_STATE", CS)
    monkeypatch.delenv("SCAN_ON_NOTIFY", raising=False)
    monkeypatch.setattr(wh, "SCAN_SETTLE_S", 0)
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(wh, "_now_ts", lambda: clock["t"])
    scans, timers = [], []

    def fake_scan(body, trigger="api"):
        scans.append((dict(body), trigger))
        return {"reports": [{"status": "ok"}], "new_failures": []}, scans_status[0]
    scans_status = [200]
    import blueprints.email as em
    monkeypatch.setattr(em, "run_mailbox_scan", fake_scan)
    monkeypatch.setattr(wh, "_schedule_trailing", lambda: timers.append(clock["t"]))
    wh._TRAILING["timer"] = None
    return {"clock": clock, "scans": scans, "timers": timers,
            "status": scans_status, "tmp": tmp_path}


def test_first_notification_scans_with_the_shared_code_path(env):
    assert wh.request_scan_on_notify(["tom.foley@usfoods.com"]) == "scanned"
    body, trigger = env["scans"][0]
    assert trigger == "webhook" and body["lookback_days"] == 2
    assert body["max_messages"] == 200


def test_burst_is_coalesced_into_one_scan_plus_one_trailing(env):
    wh.request_scan_on_notify(["a@usfoods.com"])
    for dt in (5, 60, 300):                      # more mail inside the window
        env["clock"]["t"] += dt
        assert wh.request_scan_on_notify(["b@usfoods.com"]) == "debounced"
    assert len(env["scans"]) == 1 and len(env["timers"]) == 3
    # Timer fires early on one worker: nothing. After the window: one scan.
    assert wh._trailing_fire() == "too-early"
    env["clock"]["t"] += 600
    assert wh._trailing_fire() == "scanned"
    assert wh._trailing_fire() == "nothing-pending"   # the other worker's timer
    assert len(env["scans"]) == 2


def test_notifications_before_the_scan_starts_are_covered_not_rescanned(env, monkeypatch):
    # Worker A has claimed and is settling (scan not started yet).
    monkeypatch.setattr(wh, "_run_triggered_scan", lambda: {})
    assert wh.request_scan_on_notify(["a@usfoods.com"]) == "scanned"
    env["clock"]["t"] += 10
    assert wh.request_scan_on_notify(["b@usfoods.com"]) == "covered"
    assert env["timers"] == []
    # ...but a claim that never started is taken over after a while.
    env["clock"]["t"] += 200
    assert wh.request_scan_on_notify(["b@usfoods.com"]) == "scanned"


def test_after_the_window_the_next_notification_scans_again(env):
    wh.request_scan_on_notify(["a@usfoods.com"])
    env["clock"]["t"] += 601
    assert wh.request_scan_on_notify(["a@usfoods.com"]) == "scanned"
    assert len(env["scans"]) == 2


def test_busy_ingest_lock_leaves_a_trailing_scan_pending(env):
    env["status"][0] = 409
    assert wh.request_scan_on_notify(["a@usfoods.com"]) == "scanned"
    assert len(env["timers"]) == 1
    env["status"][0] = 200
    env["clock"]["t"] += 601
    assert wh._trailing_fire() == "scanned" and len(env["scans"]) == 2


def test_env_switch_disables(env, monkeypatch):
    monkeypatch.setenv("SCAN_ON_NOTIFY", "off")
    assert wh.request_scan_on_notify(["a@usfoods.com"]) == "disabled"
    assert env["scans"] == [] and wh._trailing_fire() == "disabled"


def _notify(monkeypatch, sender, subject="PO # 054511767347"):
    monkeypatch.setattr(gs.EmailInboxClient, "_ms365_token", lambda self: "tok")
    monkeypatch.setattr(gs, "_fetch_message", lambda user, mid, token: {
        "id": mid, "subject": subject, "hasAttachments": True,
        "from": {"emailAddress": {"address": sender}},
        "receivedDateTime": "2026-11-09T14:00:05Z", "internetMessageId": "<m@x>"})
    return {"value": [{"clientState": CS,
                       "resource": "Users/jd@ms.hhbagels.com/Messages/g-1"}]}


def test_handle_notification_names_distributor_senders_only(env, monkeypatch):
    out = gs.handle_notification(_notify(monkeypatch, "MRoss@cheneybrothers.com"))
    assert out["scan_senders"] == ["mross@cheneybrothers.com"] and out["ingested"] == 0
    out = gs.handle_notification(_notify(monkeypatch, "newsletter@example.com"))
    assert out["scan_senders"] == []
    out = gs.handle_notification(_notify(monkeypatch, "bakery@hhbagels.com", "hello"))
    assert out["scan_senders"] == []


def test_worker_triggers_the_scan_and_webhook_never_500s(env, monkeypatch):
    wh._process_notifications(_notify(monkeypatch, "tom.foley@usfoods.com"))
    assert [t for _b, t in env["scans"]] == ["webhook"]
    # A trigger that blows up must not escape the worker or the endpoint.
    def boom(senders=()):
        raise RuntimeError("disk full")
    monkeypatch.setattr(wh, "request_scan_on_notify", boom)
    wh._process_notifications(_notify(monkeypatch, "tom.foley@usfoods.com"))
    # The endpoint hands the batch to a background thread and answers 202
    # without waiting for (or depending on) the scan.
    handed = []
    monkeypatch.setattr(wh, "_process_notifications", handed.append)
    from app import app
    r = app.test_client().post("/webhooks/graph/notifications",
                               json=_notify(monkeypatch, "tom.foley@usfoods.com"))
    assert r.status_code == 202
    import time
    for _ in range(50):
        if handed:
            break
        time.sleep(0.02)
    assert len(handed) == 1


def test_triggered_scan_records_feed_health_with_trigger_webhook(tmp_path, monkeypatch):
    """End to end through the real run_mailbox_scan (scanner stubbed)."""
    from core import feed_health
    from integrations import email_scanner as ES
    isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(wh, "SCAN_SETTLE_S", 0)
    monkeypatch.setattr(ES.EmailInboxClient, "scan",
                        lambda self, max_messages=300, filter_override=None:
                        ES.ScanResult(source="ms365", messages_seen=1))
    monkeypatch.setattr(ES.EmailInboxClient, "source", lambda self: "ms365")
    assert wh.request_scan_on_notify(["tom.foley@usfoods.com"]) == "scanned"
    feed = feed_health.snapshot()["feeds"]["mailbox-scan"]
    assert feed["ok"] is True and feed["trigger"] == "webhook"
    assert it.load_scan_health()["last_attempt"]["trigger"] == "webhook"
