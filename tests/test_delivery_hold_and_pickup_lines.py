"""Two misreads on the Lineage threads, 11/9 - 11/12 (the lines are real).

1. The "Lineage already answered" hold (answered_since_chase). On the 8-Orders
   thread JD asked about 1226134 alone (11/9 20:52 UTC); Belinda answered
   "Reaching out to dispatch to confirm." (21:10), naming no PO. That bare reply
   was counted for EVERY PO on the thread, so 064511780513, 1220440 (both past
   their appointments) and 9314683Y went unchased for the full 48 h.

2. The appointment reader (appointment_lines / appointment_from). Belinda's
   "014511780511-pickup 1100AM/16th and delivery is set for 1000AM/18th" was
   booked as an 11/16 11:00 delivery -- the PICKUP -- and re-booked on every
   sync after the task corrected it to 11/18 10:00.
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import blueprints.delivery as B
from integrations import po_delivery as D

BELINDA = "bmorris@onelineage.com"
JD = "jd@hhbagels.com"
THREAD_POS = ["064511780513", "014511774519", "9065675G", "9314683Y",
              "1220439", "1220440", "1226111", "1226134"]
ASK_1226134 = ("Hi team,\n- PO 1226134 (Chefs Warehouse, Bronx, NY): can you confirm it "
               "delivered at its 11/9 9:00 AM ET appointment?\nThanks,\nJD")
ASK_TWO = ("Hi team,\n- PO 064511780513 (Cheney Brothers, Punta Gorda, FL): could you send "
           "the pickup date and the delivery appointment?\n- PO 9314683Y (US Foods, "
           "Chicago, IL): could you send the pickup date and the delivery appointment?")


def _msg(sender, sent, text):
    return {"from": sender, "sent": sent, "conversation_id": "c8", "text": text}


def _chased(po, at):
    return D.record_chase({"po_number": po, "status": "scheduled", "chases": []},
                          "confirm_delivery", at, "m")


# 8-Orders thread, 11/9 (UTC)
NOV9 = [
    _msg(JD, "2026-11-09T14:51:51Z", ASK_TWO),
    _msg(BELINDA, "2026-11-09T14:54:38Z",
         "064511780513-shows picked up 1354/6th\n9314683Y-shows picked up 1354/6th"),
    _msg(JD, "2026-11-09T20:52:15Z", ASK_1226134),
    _msg(BELINDA, "2026-11-09T21:10:41Z", "Reaching out to dispatch to confirm.\n\nThanks"),
    _msg(BELINDA, "2026-11-09T21:22:57Z",
         'Per dispatch, "PO 1226134 (214113537) delivered today 11/9 - driver arrived '
         'at 9:52 and departed at 10:23"'),
]


def test_a_bare_reply_to_an_ask_about_another_po_does_not_hold_this_one():
    now = datetime(2026, 11, 10, 20, 50, tzinfo=timezone.utc)     # the 11/10 PM cron
    # 1220440 was last chased 11/6; its 11/10 10:00 AM ET appointment had passed
    rec = _chased("1220440", datetime(2026, 11, 6, 14, 52, tzinfo=timezone.utc))
    assert D.answered_since_chase(rec, NOV9, THREAD_POS, now) is None
    # 9314683Y: the 14:54 reply named it -- but our ask at 14:51 is what it
    # answered; a re-ask 30 h later is right, and the 21:10 reply is not about it
    rec = _chased("9314683Y", datetime(2026, 11, 9, 14, 51, tzinfo=timezone.utc))
    assert "14:54" in (D.answered_since_chase(rec, NOV9, THREAD_POS, now) or "")
    later = datetime(2026, 11, 11, 14, 55, tzinfo=timezone.utc)   # 14:54 is > 48 h old
    assert D.answered_since_chase(rec, NOV9, THREAD_POS, later) is None


def test_a_bare_reply_still_holds_the_pos_its_ask_was_about():
    now = datetime(2026, 11, 10, 14, 50, tzinfo=timezone.utc)
    rec = _chased("1226134", datetime(2026, 11, 9, 20, 52, tzinfo=timezone.utc))
    assert "21:10" in D.answered_since_chase(rec, NOV9[:4], THREAD_POS, now)
    # an ask about several POs, one of them this one: held, as before
    msgs = [_msg(JD, "2026-11-09T14:51:51Z", ASK_TWO),
            _msg(BELINDA, "2026-11-09T15:30:00Z", "Reaching out to dispatch to confirm.")]
    rec = _chased("064511780513", datetime(2026, 11, 9, 14, 51, tzinfo=timezone.utc))
    assert D.answered_since_chase(rec, msgs, THREAD_POS, now)


def test_a_bare_reply_with_no_specific_ask_before_it_holds_every_po():
    now = datetime(2026, 11, 10, 14, 50, tzinfo=timezone.utc)
    rec = _chased("1220440", datetime(2026, 11, 6, 14, 52, tzinfo=timezone.utc))
    # no H&H message before it, or one naming no PO: the old, cautious reading
    alone = [_msg(BELINDA, "2026-11-09T21:10:41Z", "Reaching out to dispatch to confirm.")]
    assert D.answered_since_chase(rec, alone, THREAD_POS, now)
    vague = [_msg(JD, "2026-11-09T20:52:15Z", "Any update on these? Please advise."),
             alone[0]]
    assert D.answered_since_chase(rec, vague, THREAD_POS, now)
    # a bare "thanks" in between doesn't hide the ask it follows
    thanks = [_msg(JD, "2026-11-09T20:52:15Z", ASK_1226134),
              _msg(JD, "2026-11-09T21:00:00Z", "Thanks so much"),
              alone[0]]
    assert D.answered_since_chase(rec, thanks, THREAD_POS, now) is None


PICKUP_AND_DELIVERY = ("014511780511-pickup 1100AM/16th and delivery is set for 1000AM/18th.\n"
                       "The rest was just processed to ship this week 11/13 and are LTL "
                       "shipments, dispatch work on the appts Wednesdays afternoons or "
                       "Thursday mornings, we won't have any information until tomorrow "
                       "afternoon\nBelinda Morris")


def test_the_delivery_half_of_a_pickup_and_delivery_line_is_the_appointment():
    z = "America/New_York"
    sent = "2026-11-11T21:04:07Z"
    [line] = D.appointment_lines(PICKUP_AND_DELIVERY)
    assert line.startswith("014511780511-pickup 1100AM/16th")      # quoted whole
    assert D.appointment_from(line, sent, z) == "2026-11-18T10:00:00-05:00"
    assert D.delivery_part("the driver will arrive at 1000AM/18th for pickup and delivery "
                           "is set for 0800AM/20th") == "delivery is set for 0800AM/20th"
    assert D.delivery_part("delivery set for 0800AM/20th, pickup 1000AM/18th") == \
        "delivery set for 0800AM/20th, "
    # a pickup alone is never a delivery appointment
    assert D.appointment_lines("Pickup is set for 11/16 at 11 AM") == []
    assert D.appointment_from("Pickup is set for 11/16 at 11 AM", sent, z) is None
    # lines with no pickup are read as before
    assert D.appointment_from("Delivery moved to 0830AM/18th", "2026-11-09T21:36:16Z", z) \
        == "2026-11-18T08:30:00-05:00"


def test_the_server_books_11_18_and_stops_undoing_the_correction():
    now = datetime(2026, 11, 12, 16, 0, tzinfo=timezone.utc)
    rec = {"po_number": "014511780511", "status": "scheduled",
           "requested_at": "2026-11-09T21:55:53Z",
           "delivery_appt": "2026-11-16T11:00:00-05:00",       # the misread
           "appt_evidence_sent": "2026-11-11T21:04:07Z",
           "request": {"address": "Cheney Brothers Inc, 1 Cheney Way, Riviera Beach, FL 33404."},
           "events": [], "chases": []}
    cache = {"messages": {"<b>": {"from": BELINDA, "sent": "2026-11-11T21:04:07Z",
                                  "conversation_id": "c5", "text": PICKUP_AND_DELIVERY}},
             "threads": {"c5": {"conversation_id": "c5",
                                "po_numbers": ["014511780511", "054511780512", "9751114C",
                                               "9855676H", "9919425G"]}}}
    [ev] = B._server_schedules({"014511780511": rec}, {}, cache, now)
    assert ev["delivery_appt"] == "2026-11-18T10:00:00-05:00"
    fixed, _, err = D.apply_event(rec, ev, now)
    assert not err and fixed["delivery_appt"] == "2026-11-18T10:00:00-05:00"
    # and the next sync leaves the corrected appointment alone
    assert B._server_schedules({"014511780511": fixed}, {}, cache, now) == []


def test_a_delivered_line_that_also_gives_the_pickup_dates_the_delivery():
    at = D.delivered_at_from("1209157 picked up 1556PM/13th, delivered 0651AM/21st",
                             "2026-10-21T15:00:00Z", "America/New_York")
    assert at == ("2026-10-21T06:51:00-04:00", True)
