"""Lineage delivery tracking, hardened after the 2026-10-22 review.

  * A date-only confirmation ("This shows it delivered 10/21.") keeps its DAY
    but never acts as an exact time -- the task used to fill in 12:00 and a
    09:00 count on the delivery day was "stamped before the delivery", so the
    whole PO was added on top of a count that already held it.
  * The quote must carry the date posted, and on a thread about several POs
    the message must be about THIS PO.
  * A schedule from a one-off delivery thread can't start tracking (holding)
    a PO nobody can chase.
  * Canceled / dropped POs are never chased.
  * Follow-ups: newest thread, quiet across every thread naming the PO, Graph
    calls outside the data lock, recorded as soon as the draft exists.
"""
import sys
from datetime import timedelta

sys.path.insert(0, ".")

import inventory_tracker as it
from core.util import receipt_inside_count
from integrations import po_delivery as D
from test_po_delivery import (BELINDA, KEY, NOW, ROJEAN, _inv, _requested, _routes,
                              _synced)

CHENEY = "014511763291"
CK = "plain bagel 4oz [cb - riviera beach]"
CHENEY_LEDGER = {CHENEY: {"po_number": CHENEY, "status": "in_transit",
                          "distributor": "Cheney Brothers", "warehouse": "Riviera Beach, FL"}}
DKEY = {"X-Inventory-Token": "dtok"}


def _lock_depth():
    return getattr(it._LOCK_STATE, "depth", 0)


# ---------------------------------------------------------------------------
# Date-only confirmations
# ---------------------------------------------------------------------------

def test_time_is_read_off_the_quote():
    for q in ("Yes this shows delivered 0800AM/19th", "1209205-shows delivered 1225/12th",
              "picked up 1556PM/13th", "0651AM/21st", "Appointment: 10/19/2026 8:00 AM",
              "delivered 10/19 at 1556", "delivered at 8 AM"):
        assert D.quote_has_time(q), q
    for q in ("Yes, this shows delivered 10/07.", "This shows it delivered 10/21.",
              "This shows delivered.", "delivered on 10/19/2026", "Oct 19, 2026 delivered"):
        assert not D.quote_has_time(q), q


def _delivered_rec(delivered_at, quote, **extra):
    rec, _, _ = D.apply_event(None, _requested(sent="2026-10-12T19:16:29Z"), NOW)
    rec, _, err = D.apply_event(rec, {
        "po_number": "393072B2", "event": "delivered", "delivered_at": delivered_at,
        "evidence": {"sender": BELINDA, "sent": "2026-10-19T20:00:00Z", "quote": quote},
        **extra}, NOW + timedelta(days=5))
    return rec, err


def test_the_record_says_whether_its_time_is_real():
    rec, err = _delivered_rec("2026-10-19T12:00:00-05:00", "Yes, this shows delivered 10/19.")
    assert err is None and rec["delivered_time_known"] is False
    s = D.summary(rec, NOW + timedelta(days=5))
    assert s["delivered_local"] == "2026-10-19T00:00:00" and s["delivered_time_known"] is False
    rec, _ = _delivered_rec("2026-10-19T08:00:00-05:00", "Yes this shows delivered 0800AM/19th")
    assert rec["delivered_time_known"] is True
    # an explicit flag wins over the quote
    rec, _ = _delivered_rec("2026-10-19T08:00:00-05:00", "This shows delivered.", time_known=True)
    assert rec["delivered_time_known"] is True
    # records from before 10/22 carry no flag: read off their quote
    rec, _ = _delivered_rec("2026-10-19T12:00:00-05:00", "This shows delivered.")
    rec.pop("delivered_time_known")
    assert D.delivered_time_known(rec) is False
    rec, _ = _delivered_rec("2026-10-19T08:00:00-05:00", "Yes this shows delivered 0800AM/19th")
    rec.pop("delivered_time_known")
    assert D.delivered_time_known(rec) is True


def test_a_date_the_quote_contradicts_is_refused():
    _, err = _delivered_rec("2026-10-16T12:00:00-05:00", "Yes, this shows delivered 10/15.")
    assert err and "10/16" in err and "quote says 10/15" in err
    _, err = _delivered_rec("2026-10-20T08:00:00-05:00", "Yes this shows delivered 0800AM/19th")
    assert err and "day 19" in err
    _, err = _delivered_rec("2026-10-15T12:00:00-05:00", "Yes, this shows delivered 10/15.")
    assert err is None
    _, err = _delivered_rec("2026-10-19T08:00:00-05:00", "This shows delivered.")
    assert err is None                       # no date stated: nothing to contradict


def _count_day(delivered_at, quote, **extra):
    rec, err = _delivered_rec(delivered_at, quote, **extra)
    assert err is None, err
    inv = _inv(eta="2026-10-28T00:00:00")
    item = inv[KEY]
    # Monday count at 09:00 CT already holds the 224 cs delivered that morning
    item.update(last_count_at="2026-10-19T14:00:00Z", last_count_qty=296.0,
                quantity=296.0, weekly_usage=30.0)
    return inv, rec


def test_a_made_up_noon_no_longer_adds_the_po_on_top_of_the_count(monkeypatch):
    inv, rec = _count_day("2026-10-19T12:00:00-05:00", "Yes, this shows delivered 10/19.")
    monkeypatch.setattr(it, "load_po_delivery", lambda: {"393072B2": rec})
    it._rollover_on_order(inv)
    usage = []
    it._append_rollover_usage(inv, usage)
    assert inv[KEY]["quantity"] == 296.0 and inv[KEY]["on_order"] == []
    assert usage and usage[0]["source"] == "on_order_absorbed"


def test_a_real_time_still_uses_the_exact_rule(monkeypatch):
    # Lineage said 1200PM: a 09:00 count really was before the delivery.
    inv, rec = _count_day("2026-10-19T12:00:00-05:00", "this shows delivered 1200PM/19th")
    monkeypatch.setattr(it, "load_po_delivery", lambda: {"393072B2": rec})
    it._rollover_on_order(inv)
    assert inv[KEY]["quantity"] == 520.0


def test_a_date_only_receipt_is_judged_by_day_and_quantity(monkeypatch):
    rec, _ = _delivered_rec("2026-10-19T12:00:00-05:00", "Yes, this shows delivered 10/19.")
    inv = _inv(eta="2026-10-28T00:00:00")
    inv[KEY]["last_count_at"] = "2026-10-15T14:00:00Z"        # older count
    monkeypatch.setattr(it, "load_po_delivery", lambda: {"393072B2": rec})
    it._rollover_on_order(inv)
    usage = []
    it._append_rollover_usage(inv, usage)
    row = usage[0]
    assert row["source"] == "on_order_rollover" and row["delivery_confirmed"] is True
    assert row["delivery_time_known"] is False
    assert row["arrival_date"].startswith("2026-10-19T00:00")
    # a count later the same day contains it (no exact-time override) ...
    assert receipt_inside_count(row, "2026-10-19T14:00:00Z", "2026-10-19") is True
    # ... while a row with a REAL 17:00 time is still after a 14:00 count
    real = {k: v for k, v in row.items() if k != "delivery_time_known"}
    real["arrival_date"] = "2026-10-19T17:00:00"
    assert receipt_inside_count(real, "2026-10-19T14:00:00", "2026-10-19") is False


# ---------------------------------------------------------------------------
# The quote must be about THIS PO
# ---------------------------------------------------------------------------

def _two_po_routes(monkeypatch, msgs):
    houston, _, _ = D.apply_event(None, _requested(), NOW)
    cheney, _, _ = D.apply_event(None, _requested(CHENEY), NOW)
    inv = _inv(eta="2026-10-28T00:00:00")
    inv[CK] = {"name": "Plain Bagel 4oz [CB - Riviera Beach]", "quantity": 10.0, "unit": "cs",
               "warehouse": "Riviera Beach, FL", "last_count_at": "2026-10-11T12:00:00Z",
               "on_order": [{"po_number": CHENEY, "qty": 56.0, "unit": "cs",
                             "eta": "2026-10-28T00:00:00"}]}
    return _routes(monkeypatch, inv, {"393072B2": houston, CHENEY: cheney},
                   synced=_synced(msgs, pos=("393072B2", CHENEY), base=False),
                   extra_ledger=CHENEY_LEDGER)


def _post_delivered(c, po, mid, quote, at="2026-10-15T07:52:00-04:00"):
    ev = {"po_number": po, "event": "delivered", "delivered_at": at,
          "evidence": {"message_id": mid, "quote": quote}}
    return c.post("/api/pos/delivery/events", json={"events": [ev]},
                  headers=DKEY).get_json()["results"][0]


def test_a_line_about_one_po_cannot_release_another(monkeypatch):
    msgs = {"<h@l>": {"from": BELINDA, "sent": "2026-10-15T16:00:00Z", "conversation_id": "conv1",
                      "text": "PO 393072B2 - Yes this shows delivered 0752AM/15th\n"
                              "From: JD Gross\nPO 014511763291 ..."}}
    c, store = _two_po_routes(monkeypatch, msgs)
    r = _post_delivered(c, CHENEY, "<h@l>", "Yes this shows delivered")
    assert not r["applied"] and "doesn't say which" in r["reason"]
    assert store["inv"][CK]["quantity"] == 10.0 and store["inv"][CK]["on_order"]
    r = _post_delivered(c, "393072B2", "<h@l>", "Yes this shows delivered 0752AM/15th",
                        at="2026-10-15T07:52:00-05:00")
    assert r["applied"], r


def test_a_bare_reply_counts_when_hh_asked_about_that_po_alone(monkeypatch):
    ask_one = ("Hi team,\n- PO 393072B2 (US Foods, Houston, TX): can you confirm it "
               "delivered at its 10/15 8:00 AM CT appointment?")
    ask_both = ask_one + "\n- PO 014511763291 (Cheney Brothers): could you send the pickup date"
    for ask, ok in ((ask_one, True), (ask_both, False)):
        msgs = {"<jd@hh>": {"from": "jd@hhbagels.com", "sent": "2026-10-15T15:00:00Z",
                            "conversation_id": "conv1", "text": ask},
                "<y@l>": {"from": BELINDA, "sent": "2026-10-15T16:00:00Z",
                          "conversation_id": "conv1", "text": "This shows delivered."}}
        c, store = _two_po_routes(monkeypatch, msgs)
        r = _post_delivered(c, "393072B2", "<y@l>", "This shows delivered.",
                            at="2026-10-15T07:52:00-05:00")
        assert r["applied"] is ok, (ask, r)


# ---------------------------------------------------------------------------
# One-off delivery threads can't start a hold
# ---------------------------------------------------------------------------

def _evidence_thread_routes(monkeypatch, tracking):
    synced = _synced(base=False, pos=("OTHER1",))
    synced["messages"]["<e@l>"] = {"from": BELINDA, "sent": "2026-10-14T13:58:06Z",
                                   "conversation_id": "ev1",
                                   "text": "PO 393072B2: I was able to confirm the delivery "
                                           "appt for 0700AM/15th. Update: delivered 0705AM/15th"}
    synced["threads"]["ev1"] = {"conversation_id": "ev1", "evidence_only": True,
                                "subject": "H&H Bagels PO Delivery Status",
                                "reply_to_graph_id": "", "po_numbers": ["393072B2"],
                                "last_sent": "2026-10-14T13:58:06Z"}
    return _routes(monkeypatch, _inv(eta="2026-10-14T00:00:00"), tracking, synced=synced)


SCHED_EV = {"po_number": "393072B2", "event": "scheduled",
            "delivery_appt": "2026-10-15T07:00:00-05:00",
            "evidence": {"message_id": "<e@l>", "quote": "confirm the delivery appt for 0700AM/15th"}}


def test_a_one_off_thread_schedule_cannot_start_tracking(monkeypatch):
    c, store = _evidence_thread_routes(monkeypatch, {})
    r = c.post("/api/pos/delivery/events", json={"events": [SCHED_EV]},
               headers=DKEY).get_json()["results"][0]
    assert not r["applied"] and "can't start tracking" in r["reason"]
    assert store["tracking"] == {}
    # a delivery confirmation there still records history (and releases at once)
    r = _post_delivered(c, "393072B2", "<e@l>", "delivered 0705AM/15th",
                        at="2026-10-15T07:05:00-05:00")
    assert r["applied"], r


def test_a_one_off_thread_can_still_update_a_tracked_po(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    c, store = _evidence_thread_routes(monkeypatch, {"393072B2": rec})
    r = c.post("/api/pos/delivery/events", json={"events": [SCHED_EV]},
               headers=DKEY).get_json()["results"][0]
    assert r["applied"], r


# ---------------------------------------------------------------------------
# Closed POs are never chased
# ---------------------------------------------------------------------------

def test_canceled_or_dropped_pos_are_not_chased(monkeypatch):
    import blueprints.delivery as B
    for ledger_status in ("canceled", None):
        rec, _, _ = D.apply_event(None, _requested(sent="2026-10-12T19:16:29Z"), NOW)
        c, store = _routes(monkeypatch, {}, {"393072B2": rec}, ledger_status="canceled",
                           synced=_synced(base=False))
        if ledger_status is None:          # canceled USF PO: rows removed, not in ledger
            monkeypatch.setattr(B, "_ledger_by_po", lambda: {})
        assert c.get("/api/pos/delivery", headers=DKEY).get_json()["pos"] == []
        out = c.post("/api/pos/delivery/send-followups", json={}, headers=DKEY).get_json()
        assert out["ok"] and out["sent"] == 0 and store["sent"] == [], out
        assert out["closed"] == ["393072B2"]
        assert store["tracking"]["393072B2"]["closed_reason"]
        listed = c.get("/api/pos/delivery?all=1", headers=DKEY).get_json()["pos"]
        assert listed[0]["closed_reason"] == store["tracking"]["393072B2"]["closed_reason"]


def test_closed_reason_clears_when_the_po_comes_back():
    import blueprints.delivery as B
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    tracking = {"393072B2": rec}
    assert B._stamp_closed(tracking, {}, NOW) == ["393072B2"]
    assert "ledger" in tracking["393072B2"]["closed_reason"]
    ledger = {"393072B2": {"po_number": "393072B2", "status": "in_transit"}}
    assert B._stamp_closed(tracking, ledger, NOW) == ["393072B2"]
    assert "closed_reason" not in tracking["393072B2"]
    assert B._is_open(ledger["393072B2"], tracking["393072B2"])


# ---------------------------------------------------------------------------
# Follow-ups: thread, quiet, lock, draft-first
# ---------------------------------------------------------------------------

def test_follow_up_goes_to_the_newest_thread_listing_the_po(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(sent="2026-10-12T19:16:29Z"), NOW)
    rec["thread"] = {"conversation_id": "conv1", "subject": "Quote and Schedule - 2 Orders"}
    synced = _synced(base=False)
    synced["threads"]["conv2"] = dict(synced["threads"]["conv1"], conversation_id="conv2",
                                      reply_to_graph_id="r2-jd",
                                      subject="Quote and Schedule - 3 Orders",
                                      reply_subject="RE: Quote and Schedule - 3 Orders",
                                      last_sent=(NOW - timedelta(hours=5)).isoformat())
    c, store = _routes(monkeypatch, _inv(), {"393072B2": rec}, synced=synced)
    out = c.post("/api/pos/delivery/send-followups", json={}, headers=DKEY).get_json()
    assert out["sent"] == 1 and store["sent"][0]["reply_to"] == "r2-jd", out
    assert out["emails"][0]["conversation_id"] == "conv2"


def test_a_recent_reply_on_any_thread_naming_the_po_holds_the_chase(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(sent="2026-10-12T19:16:29Z"), NOW)
    synced = _synced(base=False)
    synced["threads"]["ev1"] = {"conversation_id": "ev1", "evidence_only": True,
                                "subject": "H&H Bagels PO 393072B2 Delivery Status",
                                "reply_to_graph_id": "", "po_numbers": ["393072B2"],
                                "last_sent": (NOW - timedelta(hours=1)).isoformat()}
    synced["messages"]["<x@l>"] = {"from": ROJEAN, "conversation_id": "ev1",
                                   "sent": (NOW - timedelta(hours=1)).isoformat(),
                                   "text": "Checking with the carrier, will advise"}
    c, store = _routes(monkeypatch, _inv(), {"393072B2": rec}, synced=synced)
    out = c.post("/api/pos/delivery/send-followups", json={}, headers=DKEY).get_json()
    assert out["sent"] == 0 and store["sent"] == []
    assert "giving them time" in out["emails"][0]["held_back"]["393072B2"]


def test_graph_runs_outside_the_lock_and_concurrent_posts_survive(monkeypatch):
    import blueprints.delivery as B
    rec, _, _ = D.apply_event(None, _requested(sent="2026-10-12T19:16:29Z"), NOW)
    c, store = _routes(monkeypatch, _inv(), {"393072B2": rec}, synced=_synced(base=False))
    seen = []
    orig_send = B._send_draft

    def send(*a):
        seen.append(_lock_depth())
        # an events post lands while the email is going out
        store["tracking"] = {**store["tracking"], "OTHER": {"po_number": "OTHER",
                                                            "status": "requested"}}
        return orig_send(*a)
    monkeypatch.setattr(B, "_send_draft", send)
    orig_create = B._create_reply
    monkeypatch.setattr(B, "_create_reply",
                        lambda *a: seen.append(_lock_depth()) or orig_create(*a))
    out = c.post("/api/pos/delivery/send-followups", json={}, headers=DKEY).get_json()
    assert out["sent"] == 1 and seen == [0, 0]
    assert "OTHER" in store["tracking"], "a concurrent post must not be overwritten"
    [chase] = store["tracking"]["393072B2"]["chases"]
    assert chase["send_state"] == "sent" and chase["message_id"] == "draft-1"


def test_sync_reads_the_mailbox_outside_the_lock(monkeypatch):
    import blueprints.delivery as B
    c, store = _routes(monkeypatch, _inv(), {}, synced={})
    depths = []

    def fetch(lookback, subject=B.THREAD_SUBJECT, keep=None):
        depths.append(_lock_depth())
        if subject == B.THREAD_SUBJECT:     # an events post lands mid-read
            store["tracking"] = {"OTHER": {"po_number": "OTHER", "status": "scheduled"}}
        return [], []
    monkeypatch.setattr(B, "_fetch_thread_messages", fetch)
    out = c.post("/api/pos/delivery/sync-threads", json={}, headers=DKEY).get_json()
    assert out["ok"] and depths == [0, 0], out
    assert "OTHER" in store["tracking"]


def test_a_send_that_fails_after_the_draft_counts_as_chased(monkeypatch):
    import blueprints.delivery as B
    rec, _, _ = D.apply_event(None, _requested(sent="2026-10-12T19:16:29Z"), NOW)
    c, store = _routes(monkeypatch, _inv(), {"393072B2": rec}, synced=_synced(base=False))

    def timeout(*a):
        raise TimeoutError("timed out")
    monkeypatch.setattr(B, "_send_draft", timeout)
    out = c.post("/api/pos/delivery/send-followups", json={}, headers=DKEY).get_json()
    assert out["sent"] == 0 and out["unconfirmed_sends"] == ["conv1"]
    assert "counts as chased" in out["emails"][0]["error"]
    [chase] = store["tracking"]["393072B2"]["chases"]
    assert chase["send_state"] == "draft" and chase["message_id"] == "draft-1"
    again = c.post("/api/pos/delivery/send-followups", json={}, headers=DKEY).get_json()
    assert again["emails"] == [] and len(store["tracking"]["393072B2"]["chases"]) == 1


def test_a_failed_create_reply_leaves_nothing_recorded(monkeypatch):
    import blueprints.delivery as B
    rec, _, _ = D.apply_event(None, _requested(sent="2026-10-12T19:16:29Z"), NOW)
    c, store = _routes(monkeypatch, _inv(), {"393072B2": rec}, synced=_synced(base=False))
    good = B._create_reply

    def boom(*a):
        raise RuntimeError("createReply returned no draft id")
    monkeypatch.setattr(B, "_create_reply", boom)
    out = c.post("/api/pos/delivery/send-followups", json={}, headers=DKEY).get_json()
    assert out["sent"] == 0 and "createReply" in out["emails"][0]["error"]
    assert store["tracking"]["393072B2"]["chases"] == []
    monkeypatch.setattr(B, "_create_reply", good)
    out = c.post("/api/pos/delivery/send-followups", json={}, headers=DKEY).get_json()
    assert out["sent"] == 1                       # nothing went out: retried
