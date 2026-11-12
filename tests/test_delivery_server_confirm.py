"""The server reads plain delivery confirmations itself (JD 2026-11-04: "make
sure the webpage is being updated once the email confirmation arrives that a
PO arrived at its destination").

Before this, only the twice-a-weekday cloud task read Lineage's replies, and
7728445G (US Foods Zebulon) sat at "Awaiting delivery confirmation" for two
days after Belinda's 11/2 detention notice gave its arrival/departure times
-- the server kept asking her. The lines below are real.
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

import inventory_tracker as it
from integrations import po_delivery as D
from test_po_delivery import BELINDA, KEY, NOW, REQUEST_1012, _inv, _routes

DETENTION_1102 = (
    "Good afternoon\n\nThis is not an invoice. Please see the details below for "
    "detention unloading for PO#74428445G:\n\nDelivery Location: US Foods Raleigh\n"
    "PO: 7728445g (213795738)\nAppt Date & Time: 11/2 @ 0800\n"
    "Arrival Time: 543AM/2nd\nDeparture time: 1143AM/2nd\nDetention hours 1.5\n"
    "Detention amount $100.00 per hour\n\nBelinda Morris")


# ---------------------------------------------------------------------------
# Which lines count
# ---------------------------------------------------------------------------

def test_plain_confirmations_are_read():
    for text, line in [
        ("An email was sent Monday regarding detention unloading. This load delivered 11/2\n\nBelinda Morris",
         "This load delivered 11/2"),
        ("Yes, this shows delivered 10/07.", "Yes, this shows delivered 10/07"),
        ("This shows delivered 0900AM/22nd", "This shows delivered 0900AM/22nd"),
        ("7236706H-delivered", "7236706H-delivered"),
        ("Delivered 0752AM/15th", "Delivered 0752AM/15th"),
    ]:
        assert D.confirmation_lines(text) == [(line, "delivered")], text
    assert D.confirmation_lines(DETENTION_1102) == [("Arrival Time: 543AM/2nd", "arrival")]


def test_questions_plans_and_problems_are_left_for_a_reader():
    for text in [
        "I reached out to dispatch to confirm if this was delivered but haven't received a response.",
        'Per dispatch, "This order was not delivered. Our driver was delayed at a previous stop"',
        "Can you confirm it delivered at its 11/2 8:00 AM ET appointment?",
        "It will be delivered tomorrow.",
        "Delivered 11/2 but 2 pallets were short.",
        "Delivered, 1 pallet refused for damage.",
        "Confirmed.",
        "These orders were just processed to ship 11/6, we won't have any appts just yet.",
        "Arrival Time: 543AM/2nd",                 # no departure: not a detention notice
    ]:
        assert D.confirmation_lines(text) == [], text


def test_dates_and_times_land_in_the_docks_zone():
    z = D.dock_zone("US Foods, 1500 NC Highway 39, Zebulon, NC 27597")
    assert z == "America/New_York"
    assert D.dock_zone("", "Chicago, IL") == "America/Chicago"
    assert D.dock_zone("US Foods, 15155 Northam Street, La Mirada, CA 90638") == "America/Los_Angeles"
    sent = "2026-11-04T15:29:59Z"
    assert D.delivered_at_from("This load delivered 11/2", sent, z) == ("2026-11-02", False)
    assert D.delivered_at_from("Arrival Time: 543AM/2nd", "2026-11-02T17:33:24Z", z) == \
        ("2026-11-02T05:43:00-05:00", True)
    assert D.delivered_at_from("1209205-shows delivered 1225/12th", "2026-10-18T22:34:00Z", z) == \
        ("2026-10-12T12:25:00-04:00", True)
    # "2nd" sent on the 1st of the month is last month's 2nd? no -- never in the future
    assert D.delivered_at_from("Delivered 0800AM/30th", "2026-11-01T14:00:00Z", z)[0] \
        .startswith("2026-10-30")
    # no day: the appointment's day, only if it had passed
    assert D.delivered_at_from("This shows delivered", sent, z,
                               "2026-11-02T08:00:00-05:00") == ("2026-11-02", False)
    assert D.delivered_at_from("This shows delivered", sent, z,
                               "2026-11-06T08:00:00-05:00") is None
    assert D.delivered_at_from("This shows delivered", sent, z) is None


# ---------------------------------------------------------------------------
# The sync applies them (and the Pending tab / on-hand move)
# ---------------------------------------------------------------------------

def _sync(monkeypatch, tracking, lineage_msgs, chase_text):
    import blueprints.email as E
    c, store = _routes(monkeypatch, _inv(eta="2026-10-10T00:00:00"), tracking,
                       synced={}, extra_ledger={"014511763291": {
                           "po_number": "014511763291", "status": "in_transit",
                           "distributor": "Cheney Brothers",
                           "warehouse": "Riviera Beach, FL", "total_cs": 336.0}})
    monkeypatch.setenv("MS365_USER", "JD@ms.hhbagels.com")
    monkeypatch.setattr(E, "_graph_token", lambda: "g")
    iso = lambda h: (NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def msg(mid, h, sender, text):
        return {"id": mid, "subject": "RE: Quote and Schedule - 2 Orders",
                "conversationId": "conv1", "sentDateTime": iso(h),
                "internetMessageId": f"<{mid}>",
                "from": {"emailAddress": {"address": sender}},
                "toRecipients": [], "ccRecipients": [],
                "uniqueBody": {"content": text}}

    msgs = [msg("req", 70, "Isaiah@hhbagels.com", REQUEST_1012),
            msg("chase", 5, "JD@hhbagels.com", chase_text)]
    msgs += [msg(mid, h, sender, text) for mid, h, sender, text in lineage_msgs]
    monkeypatch.setattr(E, "_graph_json", lambda url, token, extra=None, timeout=30:
                        {"value": msgs})
    out = c.post("/api/pos/delivery/sync-threads", json={},
                 headers={"X-Inventory-Token": "tok"}).get_json()
    assert out["ok"], out
    return out, store


def _scheduled():
    rec, _, _ = D.apply_event(None, {
        "po_number": "393072B2", "event": "requested",
        "thread": {"conversation_id": "conv1", "subject": "Quote and Schedule - 2 Orders"},
        "request": {"address": "US Foods, 13400 Hollister Road, Houston, TX 77086."},
        "evidence": {"message_id": "<req>", "sent": "2026-10-12T19:16:29Z",
                     "quote": "PO #: 393072B2"}}, NOW)
    rec["status"] = "scheduled"
    rec["delivery_appt"] = "2026-10-15T08:00:00-05:00"
    return rec


CHASE_ONE = ("Hi team,\n\n- PO 393072B2 (US Foods, Houston, TX): can you confirm it "
             "delivered at its 10/15 8:00 AM CT appointment?\n\nThanks,\nJD")


def test_an_answer_to_a_one_po_chase_releases_that_po(monkeypatch):
    out, store = _sync(monkeypatch, {"393072B2": _scheduled()},
                       [("ans", 1, BELINDA, "This load delivered 10/15\n\nBelinda Morris")],
                       CHASE_ONE)
    got = out["confirmed_by_server"]
    assert [g["po_number"] for g in got if g["applied"]] == ["393072B2"], got
    rec = store["tracking"]["393072B2"]
    assert rec["status"] == "delivered" and rec["delivered_at"] == "2026-10-15"
    assert rec["delivered_time_known"] is False
    assert rec["events"][-1]["source"] == "server"
    assert rec["events"][-1]["evidence"]["sender"] == BELINDA
    # flowed through to on-hand: the held PO is no longer pending
    it.load_inventory()
    assert store["inv"][KEY]["on_order"] == []
    assert store["inv"][KEY]["quantity"] == 72.0 + 224.0
    # and the task sees the new state in the same response
    op = out["threads"][0]["open_pos"] if out["threads"] else []
    assert all((p.get("tracking") or {}).get("status") != "scheduled"
               for p in op if p["po_number"] == "393072B2")
    # a second read is a no-op
    again, _ = _sync(monkeypatch, store["tracking"],
                     [("ans", 1, BELINDA, "This load delivered 10/15\n\nBelinda Morris")],
                     CHASE_ONE)
    assert again["confirmed_by_server"] == []


def test_a_detention_notice_naming_the_po_gives_the_arrival_time(monkeypatch):
    text = DETENTION_1102.replace("7728445g", "393072b2").replace("11/2", "10/15") \
        .replace("/2nd", "/15th").replace("Raleigh", "Houston")
    out, store = _sync(monkeypatch, {"393072B2": _scheduled()},
                       [("det", 1, BELINDA, text)], "Thanks for the update.")
    rec = store["tracking"]["393072B2"]
    assert rec["status"] == "delivered", out["confirmed_by_server"]
    assert rec["delivered_at"] == "2026-10-15T05:43:00-05:00"     # Houston dock, CDT
    assert rec["delivered_time_known"] is True


def test_an_unattributed_line_on_a_two_po_thread_is_not_applied(monkeypatch):
    chase_both = ("- PO 393072B2: can you confirm it delivered?\n"
                  "- PO 014511763291: can you confirm it delivered?")
    out, store = _sync(monkeypatch, {"393072B2": _scheduled()},
                       [("ans", 1, BELINDA, "This shows delivered 10/15.")], chase_both)
    assert store["tracking"]["393072B2"]["status"] == "scheduled"
    assert not [g for g in out["confirmed_by_server"] if g.get("applied")]


def test_only_lineage_or_the_dock_can_confirm(monkeypatch):
    out, store = _sync(monkeypatch, {"393072B2": _scheduled()},
                       [("hh", 1, "Gabo@hhbagels.com", "393072B2 delivered 10/15")],
                       CHASE_ONE)
    assert store["tracking"]["393072B2"]["status"] == "scheduled"


def test_a_hedged_reply_waits_for_the_task(monkeypatch):
    out, store = _sync(monkeypatch, {"393072B2": _scheduled()},
                       [("ans", 1, BELINDA, "I reached out to dispatch to confirm if "
                                            "this was delivered. Will advise.")],
                       CHASE_ONE)
    assert store["tracking"]["393072B2"]["status"] == "scheduled"


def test_a_dry_run_sync_changes_nothing(monkeypatch):
    import blueprints.email as E  # noqa: F401
    rec = _scheduled()
    c, store = _routes(monkeypatch, _inv(), {"393072B2": rec})
    # cache already holds Belinda's answer to a one-PO chase
    store["threads"] = {
        "synced_at": NOW.isoformat(), "errors": [],
        "messages": {
            "<chase>": {"from": "JD@hhbagels.com", "sent": "2026-10-15T12:00:00Z",
                        "conversation_id": "conv1", "text": CHASE_ONE},
            "<ans>": {"from": BELINDA, "sent": "2026-10-15T17:00:00Z",
                      "conversation_id": "conv1", "text": "This load delivered 10/15"}},
        "threads": {"conv1": {"conversation_id": "conv1", "po_numbers": ["393072B2"]}}}
    import copy
    import blueprints.delivery as B
    from inventory_tracker import data_lock
    monkeypatch.setattr(it, "load_po_delivery", lambda: copy.deepcopy(store["tracking"]))
    with data_lock():
        got = B._apply_server_confirmations(dry_run=True)
    assert got and got[0]["applied"]
    assert store["tracking"]["393072B2"]["status"] == "scheduled"


# ---------------------------------------------------------------------------
# 11/5-11/6, "Quote and Schedule - 5 Orders": JD's chase asked about two POs
# (Cheney Riviera Beach 014511767346 + Ocala 054511767347); Belinda answered
# "Both shows delivered today". The server wanted a one-PO chase, so neither
# released, and the next morning's run asked her again (her out-of-office
# came straight back).
# ---------------------------------------------------------------------------

BOTH_1105 = "Both shows delivered today\n\nBelinda Morris\nLogistics Specialist, Senior"
OOO_1106 = ("Please be advised that I will be out of the office Friday 11/6/2026 and I "
            "will return Monday 11/9/2026 at 0830AM. If you need immediate assistance, "
            "please contact ltranssolutionseast@onelineage.com for any issues or concerns.")
CHASE_BOTH = ("Hi team,\n\n- PO 014511763291 (Cheney Brothers, Riviera Beach, FL): can you "
              "confirm it delivered at its 10/15 9:00 AM ET appointment?\n- PO 393072B2 "
              "(US Foods, Houston, TX): can you confirm it delivered at its 10/15 8:00 AM "
              "CT appointment?\n\nThanks,\nJD")


def _scheduled_cheney():
    rec, _, _ = D.apply_event(None, {
        "po_number": "014511763291", "event": "requested",
        "thread": {"conversation_id": "conv1", "subject": "Quote and Schedule - 2 Orders"},
        "request": {"address": "Cheney Brothers Inc, 1 Cheney Way, Riviera Beach, FL 33404"},
        "evidence": {"message_id": "<req>", "sent": "2026-10-12T19:16:29Z",
                     "quote": "PO #: 014511763291"}}, NOW)
    rec["status"] = "scheduled"
    rec["delivery_appt"] = "2026-10-15T09:00:00-04:00"
    return rec


def test_both_delivered_answering_a_two_po_chase_releases_both(monkeypatch):
    out, store = _sync(monkeypatch,
                       {"393072B2": _scheduled(), "014511763291": _scheduled_cheney()},
                       [("ans", 1, BELINDA, BOTH_1105)], CHASE_BOTH)
    got = sorted(g["po_number"] for g in out["confirmed_by_server"] if g["applied"])
    assert got == ["014511763291", "393072B2"], out["confirmed_by_server"]
    for k in ("393072B2", "014511763291"):
        rec = store["tracking"][k]
        assert rec["status"] == "delivered" and rec["delivered_at"] == "2026-10-15"
        assert rec["events"][-1]["evidence"]["quote"] == "Both shows delivered today"


def test_a_collective_reply_only_covers_the_pos_that_were_asked(monkeypatch):
    # the chase asked about 393072B2 alone, so "Both ..." can't release the other PO
    out, store = _sync(monkeypatch,
                       {"393072B2": _scheduled(), "014511763291": _scheduled_cheney()},
                       [("ans", 1, BELINDA, BOTH_1105)], CHASE_ONE)
    assert store["tracking"]["393072B2"]["status"] == "delivered"
    assert store["tracking"]["014511763291"]["status"] == "scheduled"


def test_today_and_yesterday_read_off_the_send_day():
    z = "America/New_York"
    assert D.delivered_at_from("Both shows delivered today", "2026-11-05T20:55:51Z", z) == \
        ("2026-11-05", False)
    assert D.delivered_at_from("It delivered yesterday", "2026-11-06T14:00:00Z", z) == \
        ("2026-11-05", False)
    assert D.confirmation_lines(BOTH_1105) == [("Both shows delivered today", "delivered")]


def test_out_of_office_is_an_auto_reply():
    assert D.is_auto_reply({"subject": "Automatic reply: Quote and Schedule - 5 Orders",
                            "text": OOO_1106})
    assert D.is_auto_reply({"subject": "RE: Quote and Schedule - 5 Orders", "text": OOO_1106})
    assert not D.is_auto_reply({"subject": "RE: Quote and Schedule - 5 Orders",
                                "text": BOTH_1105})


# Belinda 11/12/2026 10:01 ET, the real reply on the 8-Orders thread
MIXED_1112 = ("064511780513-shows delivered\n1220440-shows delivered\n"
              "9314683Y-had to reach out to dispatch to confirm if delivered\n\n"
              "Belinda Morris\nLogistics Specialist, Senior")


def test_a_hedged_po_line_holds_back_only_its_own_po():
    assert D.confirmation_lines(MIXED_1112) == [
        ("064511780513-shows delivered", "delivered"),
        ("1220440-shows delivered", "delivered")]
    # an unscoped hedge still discards the whole message
    assert D.confirmation_lines("064511780513-shows delivered\n"
                                "Not sure if the other one delivered") == []
    # once a PO line hedged, an unscoped "delivered" line is not trusted
    assert D.confirmation_lines("9314683Y-confirming if delivered\n"
                                "This shows delivered") == []
    # no PO named and no time: the day comes from the passed appointment
    assert D.delivered_at_from("064511780513-shows delivered", "2026-11-12T15:01:53Z",
                               "America/New_York", "2026-11-11T08:30:00-05:00") == \
        ("2026-11-11", False)


def test_mixed_reply_releases_the_confirmed_po_and_holds_the_hedged_one(monkeypatch):
    lines = ("393072B2-shows delivered\n"
             "014511763291-had to reach out to dispatch to confirm if delivered\n\n"
             "Belinda Morris")
    out, store = _sync(monkeypatch,
                       {"393072B2": _scheduled(), "014511763291": _scheduled_cheney()},
                       [("ans", 1, BELINDA, lines)], CHASE_BOTH)
    got = [g["po_number"] for g in out["confirmed_by_server"] if g["applied"]]
    assert got == ["393072B2"], out["confirmed_by_server"]
    assert store["tracking"]["393072B2"]["status"] == "delivered"
    assert store["tracking"]["393072B2"]["delivered_at"] == "2026-10-15"
    assert store["tracking"]["014511763291"]["status"] == "scheduled"
