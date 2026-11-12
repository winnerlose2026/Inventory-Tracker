"""A delivery confirmation with no date still marks the PO arrived.

JD 11/12: "I want to make sure that when we receive a delivered confirmation,
that it doesn't get hung because it's missing a delivery date. It's more
important that it's marked as arrived than getting the exact arrival date
correct, which can always be corrected later if needed."

Chicago 9314683Y: picked up 11/6, no appointment ever booked; Belinda, 11/12
10:46 AM ET: "9314683Y-delivered". With no day and no appointment to fall
back on, the server left it held in Pending, and with her reply on the thread
nothing was going to chase it either. The lines below are real.
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

import blueprints.delivery as B
from integrations import lead_times as LT
from integrations import po_delivery as D

BELINDA = "bmorris@onelineage.com"
PO = "9314683Y"
CHI = "America/Chicago"
NOW = datetime(2026, 11, 12, 17, 0, tzinfo=timezone.utc)
POS = ["064511780513", "014511774519", "9065675G", "9314683Y", "1220439", "1220440",
       "1226111", "1226134"]
ASK = ("Hi team,\n- PO 064511780513 (Cheney Brothers, Punta Gorda, FL): can you confirm "
       "it delivered at its 11/11 8:30 AM ET appointment?\n- PO 1220440 (Chefs Warehouse, "
       "Hanover, MD): can you confirm it delivered at its 11/10 10:00 AM ET appointment?\n"
       "- PO 9314683Y (US Foods, Chicago, IL): could you send the pickup date and the "
       "delivery appointment (date and time)?\nThanks,\nJD")
MIXED = ("064511780513-shows delivered\n1220440-shows delivered\n9314683Y-had to reach out "
         "to dispatch to confirm if delivered\n\nBelinda Morris")
DELIVERED = "9314683Y-delivered\nBelinda Morris\nLogistics Specialist, Senior"


def _rec(**kw):
    return {"po_number": PO, "status": "requested", "requested_at": "2026-10-30T20:59:32Z",
            "pickup_at": "2026-11-06T13:54:00-05:00",
            "request": {"address": "US Foods, 800 Supreme Dr, Bensenville, IL 60106"},
            "events": [], "chases": [], **kw}


def _cache(*msgs):
    return {"messages": {mid: {"from": f, "sent": s, "conversation_id": "c8", "text": t}
                         for mid, f, s, t in msgs},
            "threads": {"c8": {"conversation_id": "c8", "po_numbers": POS}}}


NOV12 = [("<ask>", "JD@hhbagels.com", "2026-11-12T14:52:21Z", ASK),
         ("<mixed>", BELINDA, "2026-11-12T15:01:53Z", MIXED),
         ("<del>", BELINDA, "2026-11-12T15:46:02Z", DELIVERED)]


def _ledger():
    return {PO: {"po_number": PO, "warehouse": "Chicago, IL", "distributor": "US Foods",
                 "status": "in_transit"}}


def test_an_undated_confirmation_marks_it_arrived_by_the_day_it_was_sent():
    assert not D.states_a_day("9314683Y-delivered")
    assert D.delivered_at_from("9314683Y-delivered", "2026-11-12T15:46:02Z", CHI) is None
    assert D.delivered_by_sent_day("2026-11-12T15:46:02Z", CHI) == "2026-11-12"
    # the send day in the DOCK's zone: 11:30 PM CT on the 11th is still the 11th
    assert D.delivered_by_sent_day("2026-11-12T05:30:00Z", CHI) == "2026-11-11"

    [ev] = B._server_confirmations({PO: _rec()}, _ledger(), _cache(*NOV12), NOW)
    assert ev["evidence"]["message_id"] == "<del>"          # not the hedged line
    assert ev["delivered_at"] == "2026-11-12" and ev["time_known"] is False
    assert ev["date_estimated"] is True
    rec, change, err = D.apply_event(_rec(), {**ev, "evidence": {**ev["evidence"]}}, NOW)
    assert not err and change and rec["status"] == "delivered"
    assert rec["delivered_date_estimated"] is True and rec["delivered_time_known"] is False
    s = D.summary(rec, NOW)
    assert s["delivered_date_estimated"] is True
    assert s["delivered_local"] == "2026-11-12T00:00:00"
    # the next read is a no-op: the same line can't move it
    assert B._server_confirmations({PO: rec}, _ledger(), _cache(*NOV12), NOW) == []


def test_a_later_message_with_the_day_corrects_it_but_never_later():
    rec, _, _ = D.apply_event(_rec(), {
        "po_number": PO, "event": "delivered", "delivered_at": "2026-11-12",
        "time_known": False, "date_estimated": True,
        "evidence": {"message_id": "<del>", "sender": BELINDA,
                     "sent": "2026-11-12T15:46:02Z", "quote": "9314683Y-delivered"}}, NOW)
    later = datetime(2026, 11, 13, 17, 0, tzinfo=timezone.utc)
    # another undated "delivered", or a day AFTER the estimate: ignored
    for text in ("9314683Y-delivered", "9314683Y-delivered 11/13"):
        cache = _cache(*NOV12, ("<x>", BELINDA, "2026-11-13T15:00:00Z", text))
        assert B._server_confirmations({PO: rec}, _ledger(), cache, later) == [], text
    # the real day (and time) replaces the estimate
    cache = _cache(*NOV12, ("<day>", BELINDA, "2026-11-13T15:00:00Z",
                            "9314683Y-delivered 0730AM/10th"))
    [ev] = B._server_confirmations({PO: rec}, _ledger(), cache, later)
    assert ev["delivered_at"] == "2026-11-10T07:30:00-06:00" and "date_estimated" not in ev
    fixed, change, err = D.apply_event(rec, ev, later)
    assert not err and change
    assert fixed["delivered_date_estimated"] is False and fixed["delivered_time_known"] is True
    assert not D.delivered_date_estimated(fixed)
    # an undated confirmation never moves a confirmed date
    _, _, err = D.apply_event(fixed, {**ev, "delivered_at": "2026-11-13",
                                      "date_estimated": True}, later)
    assert err and "doesn't move its date" in err


def test_a_passed_appointment_or_a_stated_day_still_wins_over_the_send_day():
    # undated, but the appointment had passed: the appointment's day, as before
    rec = _rec(status="scheduled", delivery_appt="2026-11-10T08:00:00-06:00")
    [ev] = B._server_confirmations({PO: rec}, _ledger(), _cache(*NOV12), NOW)
    assert ev["delivered_at"] == "2026-11-10" and "date_estimated" not in ev
    # an older undated line and a newer one that states the day: the stated day
    cache = _cache(*NOV12, ("<day>", BELINDA, "2026-11-12T16:30:00Z",
                            "9314683Y-shows delivered 11/10"))
    [ev] = B._server_confirmations({PO: _rec()}, _ledger(), cache, NOW)
    assert ev["delivered_at"] == "2026-11-10" and ev["evidence"]["message_id"] == "<day>"


def test_hedged_or_problem_lines_still_dont_count():
    for text in ("9314683Y-had to reach out to dispatch to confirm if delivered",
                 "9314683Y-not delivered yet", "9314683Y-delivered short 2 cases"):
        cache = _cache(NOV12[0], ("<h>", BELINDA, "2026-11-12T15:46:02Z", text))
        assert B._server_confirmations({PO: _rec()}, _ledger(), cache, NOW) == [], text


def test_a_record_posted_before_the_flag_is_read_off_its_quote():
    """9314683Y was posted by hand on 11/12 (delivered_at "2026-11-12", no
    flag). 1220440 was dated by its passed appointment: not an estimate."""
    def posted(po, quote, appt=""):
        return {"po_number": po, "status": "delivered", "delivered_at": "2026-11-12",
                "delivered_time_known": False, "delivery_appt": appt,
                "events": [{"event": "delivered", "evidence": {"quote": quote}}]}
    hand = posted(PO, "9314683Y-delivered")
    assert D.delivered_date_estimated(hand)
    assert D.summary(hand, NOW)["delivered_date_estimated"] is True
    assert not D.delivered_date_estimated(posted("1220440", "1220440-shows delivered",
                                                 "2026-11-10T10:00:00-05:00"))
    assert not D.delivered_date_estimated(posted("7402733Y",
                                                 "Per the carrier this delivered yesterday"))
    # and the first message that states the day corrects the hand-posted one
    later = datetime(2026, 11, 13, 17, 0, tzinfo=timezone.utc)
    cache = _cache(*NOV12, ("<day>", BELINDA, "2026-11-13T15:00:00Z",
                            "9314683Y-delivered 11/10"))
    [ev] = B._server_confirmations({PO: {**_rec(), **hand}}, _ledger(), cache, later)
    assert ev["delivered_at"] == "2026-11-10"


def test_an_estimated_date_is_not_a_lead_time_sample():
    rec, _, _ = D.apply_event(_rec(), {
        "po_number": PO, "event": "delivered", "delivered_at": "2026-11-12",
        "time_known": False, "date_estimated": True,
        "evidence": {"message_id": "<del>", "sender": BELINDA,
                     "sent": "2026-11-12T15:46:02Z", "quote": "9314683Y-delivered"}}, NOW)
    samples, excluded = LT.collect_samples(
        {PO: rec}, {PO: {"distributor": "US Foods", "warehouse": "Chicago, IL"}}, {},
        D.norm_po, D.wall_time)
    assert samples == []
    assert excluded[0]["reason"].startswith("delivery date not given")
