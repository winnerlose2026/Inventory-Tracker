"""The server reads firm appointment changes itself (JD 2026-11-10: "I want
these updated arrival dates to automatically get updated in the dashboard as
they are sent").

Cheney's Marbel Fox wrote "We show this PO scheduled for 11/11 at 8:30 AM" on
a one-off "PO 064511780513 - Delivery appointment help" thread; the Pending
tab kept Lineage's earlier 11/18 until it was posted by hand. The lines below
are real.
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

import blueprints.delivery as B
from integrations import po_delivery as D

NOW = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)
PO = "064511780513"
MARBEL = "marbelf@cheneybrothers.com"
BELINDA = "bmorris@onelineage.com"
MARBEL_1109 = ("We show this PO scheduled for 11/11 at 8:30 AM\n\nMarbel Fox\nManager Category\n"
               "________________________________\nFrom: JD Gross <JD@hhbagels.com>\n"
               "Could you help us secure a confirmed 8:30 AM appointment on the 11th?")
BELINDA_1109 = ("064511780513-per dispatch, they requested 0830AM/11th for delivery appt but "
                "received 0830AM/18th due to capacity issues. If you are able to assist us with "
                "a confirmed delivery appt for 0830AM/11th we can deliver on this day. If not, "
                "this will deliver 0830AM/18th.\n\nThanks\nBelinda Morris")


def _rec():
    return {"po_number": PO, "status": "scheduled",
            "requested_at": "2026-11-02T15:00:00Z",
            "delivery_appt": "2026-11-18T08:30:00-05:00",
            "appt_evidence_sent": "2026-11-05T15:00:00Z",
            "request": {"address": "Cheney Brothers, 1 Cheney Way, Punta Gorda, FL 33982"},
            "events": [], "chases": []}


def _cache(msgs, pos=(PO,), evidence_only=True):
    return {"messages": {mid: {"from": f, "sent": s, "conversation_id": "c1", "text": t}
                         for mid, f, s, t in msgs},
            "threads": {"c1": {"conversation_id": "c1", "po_numbers": list(pos),
                               "evidence_only": evidence_only}}}


def test_firm_lines_are_read_and_conditionals_are_not():
    assert D.appointment_lines(MARBEL_1109) == ["We show this PO scheduled for 11/11 at 8:30 AM"]
    assert D.appointment_lines("Delivery moved to Monday.\nAppointment: 10/19/2026 8:00 AM\n"
                               "Appointment Confirmation #: 18021721") == \
        ["Appointment: 10/19/2026 8:00 AM"]
    for text in [BELINDA_1109,
                 "Can you schedule this for 11/12 at 9 AM?",
                 "No, this order was not delivered. This was picked up 11/03.",
                 "This shows delivered 0900AM/11th",
                 "Noted\n\nThanks",
                 "Scheduled for 11/11 8:30 AM. Appointment 11/12 9:00 AM."]:   # disagree
        assert D.appointment_lines(text) == [], text


def test_days_resolve_forward_in_the_docks_zone():
    z = "America/New_York"
    assert D.appointment_from("scheduled for 11/11 at 8:30 AM", "2026-11-09T21:36:16Z", z) \
        == "2026-11-11T08:30:00-05:00"
    assert D.appointment_from("Delivery moved to 0830AM/18th", "2026-11-09T21:36:16Z", z) \
        == "2026-11-18T08:30:00-05:00"
    assert D.appointment_from("Appointment set for 1/4 at 7 AM", "2026-12-28T15:00:00Z", z) \
        == "2027-01-04T07:00:00-05:00"
    # no time: keep the appointment's time; with none on file, read nothing
    assert D.appointment_from("Rescheduled for 11/12", "2026-11-09T21:36:16Z", z,
                              "2026-11-18T08:30:00-05:00") == "2026-11-12T08:30:00-05:00"
    assert D.appointment_from("Rescheduled for 11/12", "2026-11-09T21:36:16Z", z) is None


def test_the_dcs_new_appointment_replaces_lineages(monkeypatch):
    cache = _cache([("<b>", BELINDA, "2026-11-09T18:44:00Z", BELINDA_1109),
                    ("<m>", MARBEL, "2026-11-09T21:36:16Z", MARBEL_1109)])
    evs = B._server_schedules({PO: _rec()}, {}, cache, NOW)
    assert len(evs) == 1
    assert evs[0]["event"] == "rescheduled"
    assert evs[0]["delivery_appt"] == "2026-11-11T08:30:00-05:00"
    assert evs[0]["evidence"]["message_id"] == "<m>"
    rec, change, err = D.apply_event(_rec(), {**evs[0], "evidence": {
        **evs[0]["evidence"], "sender": MARBEL}}, NOW)
    assert not err and rec["delivery_appt"] == "2026-11-11T08:30:00-05:00"


def test_a_second_read_is_a_no_op():
    rec = _rec()
    rec["delivery_appt"] = "2026-11-11T08:30:00-05:00"
    rec["appt_evidence_sent"] = "2026-11-09T21:36:16Z"
    cache = _cache([("<m>", MARBEL, "2026-11-09T21:36:16Z", MARBEL_1109)])
    assert B._server_schedules({PO: rec}, {}, cache, NOW) == []


def test_hh_staff_and_unattributed_multi_po_lines_dont_move_it():
    cache = _cache([("<h>", "Isaiah@hhbagels.com", "2026-11-09T21:36:16Z", MARBEL_1109)])
    assert B._server_schedules({PO: _rec()}, {}, cache, NOW) == []
    cache = _cache([("<m>", MARBEL, "2026-11-09T21:36:16Z", MARBEL_1109)],
                   pos=(PO, "054511780512"))
    assert B._server_schedules({PO: _rec()}, {}, cache, NOW) == []


def test_delivered_or_untracked_pos_are_left_alone():
    cache = _cache([("<m>", MARBEL, "2026-11-09T21:36:16Z", MARBEL_1109)])
    rec = {**_rec(), "status": "delivered", "delivered_at": "2026-11-10T08:00:00-05:00"}
    assert B._server_schedules({PO: rec}, {}, cache, NOW) == []
    assert B._server_schedules({}, {}, cache, NOW) == []
