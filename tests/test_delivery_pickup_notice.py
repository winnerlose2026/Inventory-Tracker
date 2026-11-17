"""Gabo gets only the Woodside pickup dates (JD 11/17), not the follow-ups.

The server reads Lineage's firm pickup lines ("PO#...-Pickup 1100AM/16th and
delivery 1000AM/18th"), records them, and emails the bakery once per new or
changed pickup that's still ahead.
"""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from integrations import po_delivery as D
from test_po_delivery import BELINDA, NOW, REQUEST_1012
from test_delivery_forked_threads import BASE, _msg, _run
from test_delivery_server_confirm import _scheduled


def _req():
    return _msg("req", 70, "Isaiah@hhbagels.com", REQUEST_1012, BASE, "conv1")


def _future_sched():
    rec = _scheduled()
    rec["delivery_appt"] = "2026-10-20T08:00:00-05:00"   # still ahead of NOW (10/15)
    return rec


def _sent(monkeypatch):
    import blueprints.delivery as B
    sent = []
    monkeypatch.setattr(B, "_send_new_mail",
                        lambda frm, to, cc, subj, body: sent.append(
                            {"from": frm, "to": list(to), "cc": list(cc),
                             "subject": subj, "body": body}))
    return sent


def _go(monkeypatch, msgs):
    import test_delivery_forked_threads as F
    sent = _sent(monkeypatch)
    monkeypatch.setattr(F, "_scheduled", _future_sched)
    out, store, _q = _run(monkeypatch, msgs)
    return out, store, sent


def test_gabo_is_off_the_followups():
    assert not any("gabo" in a.lower() for a in D.FOLLOWUP_TO + D.FOLLOWUP_CC)
    assert D.pickup_notice_to() == ("Gabo@hhbagels.com",)


def test_pickup_line_is_read_and_gabo_is_told_once(monkeypatch):
    msgs = [_req(), _msg("bm", 2, BELINDA,
                         "PO#393072B2-Pickup 1100AM/17th and delivery 1000AM/20th\n"
                         "Belinda Morris", BASE, "conv1")]
    out, store, sent = _go(monkeypatch, msgs)
    rec = store["tracking"]["393072B2"]
    assert rec["pickup_at"] == "2026-10-17T11:00:00-04:00"
    assert rec["status"] == "scheduled"               # a pickup doesn't change status
    [mail] = sent
    assert mail["to"] == ["Gabo@hhbagels.com"] and mail["cc"] == []
    assert "Woodside pickup: PO 393072B2 - Saturday 10/17, 11:00 AM" == mail["subject"]
    assert "ready for departure" in mail["body"] and "1120 cases" in mail["body"]
    assert out["pickup_notice"]["sent"] is True
    # the next sync says nothing new
    out2, _store2, _q = _run_again(monkeypatch, msgs, store)
    assert len(sent) == 1


def _run_again(monkeypatch, msgs, store):
    import blueprints.email as E
    from app import app
    monkeypatch.setattr(E, "_graph_json", lambda url, token, extra=None, timeout=30:
                        {"value": msgs})
    out = app.test_client().post("/api/pos/delivery/sync-threads", json={},
                                 headers={"X-Inventory-Token": "tok"}).get_json()
    return out, store, None


def test_a_changed_pickup_is_sent_again_marked_changed(monkeypatch):
    msgs = [_req(), _msg("bm", 5, BELINDA, "PO#393072B2-Pickup 1100AM/17th\nBelinda",
                         BASE, "conv1")]
    _out, store, sent = _go(monkeypatch, msgs)
    msgs.append(_msg("bm2", 1, BELINDA, "393072B2 pickup moved to 0900AM/19th\nBelinda",
                     BASE, "conv1"))
    _run_again(monkeypatch, msgs, store)
    assert len(sent) == 2
    assert sent[1]["subject"].startswith("UPDATED Woodside pickup: PO 393072B2 - Monday 10/19")
    assert "CHANGED - was Saturday 10/17, 11:00 AM" in sent[1]["body"]


def test_a_failed_send_retries_next_sync(monkeypatch):
    import blueprints.delivery as B
    msgs = [_req(), _msg("bm", 2, BELINDA, "PO#393072B2-Pickup 1100AM/17th\nBelinda",
                         BASE, "conv1")]
    calls = []

    def boom(*a):
        calls.append(1)
        raise TimeoutError("graph down")
    import test_delivery_forked_threads as F
    monkeypatch.setattr(F, "_scheduled", _future_sched)
    monkeypatch.setattr(B, "_send_new_mail", boom)
    out, store, _q = _run(monkeypatch, msgs)
    assert out["pickup_notice"]["sent"] is False and "pickup_notice" not in store["tracking"]["393072B2"]
    sent = _sent(monkeypatch)
    _run_again(monkeypatch, msgs, store)
    assert len(sent) == 1


def test_past_pickups_and_actual_pickups_send_nothing(monkeypatch):
    msgs = [_req(), _msg("bm", 2, BELINDA, "393072B2 was picked up 1100AM/14th\nBelinda",
                         BASE, "conv1")]
    _out, _store, sent = _go(monkeypatch, msgs)
    assert sent == []


def test_an_older_line_never_undoes_a_newer_pickup():
    rec = _future_sched()
    # the task set 4 PM off an 11/16-style later note
    rec, ch, err = D.apply_event(rec, {
        "po_number": "393072B2", "event": "scheduled",
        "delivery_appt": rec["delivery_appt"], "pickup_at": "2026-10-17T16:00:00-04:00",
        "evidence": {"message_id": "late", "sender": BELINDA,
                     "sent": "2026-10-15T17:00:00Z", "quote": "arrive at 4Pm for pickup"}}, NOW)
    assert ch and not err
    _r, ch2, err2 = D.apply_event(rec, {
        "po_number": "393072B2", "event": "pickup_scheduled",
        "pickup_at": "2026-10-17T11:00:00-04:00",
        "evidence": {"message_id": "early", "sender": BELINDA,
                     "sent": "2026-10-14T12:00:00Z", "quote": "Pickup 1100AM/17th"}}, NOW)
    assert ch2 is None and "stale" in err2


def test_a_same_day_line_without_a_time_keeps_the_time():
    rec = {**_future_sched(), "pickup_at": "2026-10-17T12:00:00-04:00"}
    _r, ch, err = D.apply_event(rec, {
        "po_number": "393072B2", "event": "pickup_scheduled", "pickup_at": "2026-10-17",
        "evidence": {"message_id": "x", "sender": BELINDA,
                     "sent": "2026-10-15T12:00:00Z", "quote": "Pick up scheduled 10/17"}}, NOW)
    assert ch is None and err is None
