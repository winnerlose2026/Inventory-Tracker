"""PO labels on the multi-PO Quote and Schedule threads (JD 2026-11-11).

The 5-Orders thread of 11/6-11/11 carried 7135734C (USF La Mirada) and two
Cheney POs. Belinda's 11/11 answer named no PO: "This order is set for delivery
at 11/13 @0900AM." The lines below are real.
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

import blueprints.delivery as B
from blueprints import pos as P
from integrations import po_delivery as D

BELINDA = "bmorris@onelineage.com"
JD = "jd@hhbagels.com"
USF, RB, OC = "7135734C", "014511767346", "054511767347"
POS = [USF, RB, OC]
ASK_USF = ("Hi Belinda / team,\nURGENT: PO 7135734C (US Foods, 15155 Northam Street, "
           "La Mirada, CA, 6 pallets / 336 cases)\nThis load is now 5 days past its 11/6 "
           "8:00 AM PT appointment. Please send us today, as soon as possible: the confirmed "
           "delivery appointment at La Mirada.")
ASK_TWO = ("Hi team,\n- PO 014511767346 (Cheney Brothers, Riviera Beach, FL): can you confirm "
           "it delivered at its 11/5 9:00 AM ET appointment?\n- PO 054511767347 (Cheney "
           "Brothers, Ocala, FL): can you confirm it delivered at its 11/5 8:00 AM ET "
           "appointment?")
DELAY = ("Good morning\nMy apologies for the delay in a response. I have been reaching out "
         "to dispatch regarding the status. Hopefully I have something soon.\nThanks")
SET = "Good afternoon\nThis order is set for delivery at 11/13 @0900AM.\nThanks"
THANKS = ("Thanks so much for getting us the info.\nBest,\nJD\nJD Gross | Director of Bakery "
          "Operations | H&H Bagels")


def _cache(msgs, pos=POS):
    return {"messages": {mid: {"from": f, "sent": s, "conversation_id": "c5", "text": t,
                               "mailbox": "jd@hhbagels.com", "graph_id": "g" + mid,
                               "categories": []}
                         for mid, f, s, t in msgs},
            "threads": {"c5": {"conversation_id": "c5", "po_numbers": list(pos),
                               "subject": "Re: Quote and Schedule - 5 Orders"}}}


LEDGER = {USF: {"po_number": USF, "distributor": "US Foods", "warehouse": "La Mirada, CA"},
          RB: {"po_number": RB, "distributor": "Cheney Brothers", "warehouse": "Riviera Beach, FL"},
          OC: {"po_number": OC, "distributor": "Cheney Brothers", "warehouse": "Ocala, FL"}}


def test_labels_name_the_po_and_destination():
    assert D.po_label(USF, "US Foods", "La Mirada") == "PO 7135734C · USF La Mirada"
    assert D.po_label("1220439", "Chefs Warehouse", "Opa Locka") == "PO 1220439 · CW Opa Locka"
    assert D.po_label("X1") == "PO X1"
    assert D.short_place("La Mirada, CA") == "La Mirada"
    assert D.short_place("", "US Foods, 15155 Northam Street, La Mirada, CA 90638") == "La Mirada"
    assert D.short_place("", "Cheney Brothers Inc, 2801 W Silver Springs BLVD, Ocala FL 34475") == "Ocala"
    assert D.short_place("", "US Foods, 269 Kings Ct, Alcoa, TN. 37701") == "Alcoa"


def test_belindas_bare_answer_is_labeled_for_the_po_jd_asked_about():
    cache = _cache([("a", JD, "2026-11-11T15:15:00Z", ASK_USF),
                    ("b", BELINDA, "2026-11-11T15:35:00Z", DELAY),
                    ("c", BELINDA, "2026-11-11T16:35:00Z", SET)])
    labels, unmatched = B._message_plan(cache, {}, LEDGER)
    want = ["PO 7135734C · USF La Mirada"]
    assert labels["a"] == want and labels["b"] == want and labels["c"] == want
    assert unmatched == {}


def test_a_thank_you_in_between_no_longer_breaks_the_match():
    cache = _cache([("a", JD, "2026-11-11T15:15:00Z", ASK_USF),
                    ("t", JD, "2026-11-11T16:00:00Z", THANKS),
                    ("c", BELINDA, "2026-11-11T16:35:00Z", SET)])
    labels, unmatched = B._message_plan(cache, {}, LEDGER)
    assert labels["c"] == ["PO 7135734C · USF La Mirada"]
    assert "t" not in labels
    # and the server's own schedule read now credits it to 7135734C
    rec = {"po_number": USF, "status": "requested", "requested_at": "2026-10-28T15:00:00Z",
           "request": {"address": "US Foods, 15155 Northam Street, La Mirada, CA 90638"},
           "events": [], "chases": []}
    evs = B._server_schedules({USF: rec}, LEDGER, cache,
                              datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc))
    assert [e["delivery_appt"] for e in evs] == ["2026-11-13T09:00:00-08:00"]


def test_a_question_in_between_is_still_the_message_being_answered():
    q = "Can you give us an update on all of these?"
    cache = _cache([("a", JD, "2026-11-11T15:15:00Z", ASK_USF),
                    ("q", JD, "2026-11-11T16:00:00Z", q),
                    ("c", BELINDA, "2026-11-11T16:35:00Z", SET)])
    labels, unmatched = B._message_plan(cache, {}, LEDGER)
    assert labels["c"] == [D.NEEDS_REVIEW_LABEL]
    assert unmatched["c5"][0]["quote"] == "This order is set for delivery at 11/13 @0900AM"


def test_a_collective_answer_gets_every_po_it_covers():
    cache = _cache([("a", JD, "2026-11-06T14:52:00Z", ASK_TWO),
                    ("b", BELINDA, "2026-11-06T16:46:00Z", "I sent this information yesterday. Both delivered")])
    labels, _ = B._message_plan(cache, {}, LEDGER)
    assert labels["b"] == ["PO 014511767346 · Cheney Riviera Beach",
                           "PO 054511767347 · Cheney Ocala"]


def test_an_unmatched_date_shows_under_held_pos_until_something_newer():
    cache = _cache([("q", JD, "2026-11-11T16:00:00Z", "Any update on these?"),
                    ("c", BELINDA, "2026-11-11T16:35:00Z", SET)])
    _, unmatched = B._message_plan(cache, {}, LEDGER)
    cache["threads"]["c5"]["unmatched"] = unmatched["c5"]
    by_po = P._unmatched_by_po(cache)
    now = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)
    held = {"status": "requested", "events": [{"evidence": {"sent": "2026-11-10T12:00:00Z"}}]}
    assert [n["quote"] for n in P._unmatched_for(held, by_po[USF], now)] == \
        ["This order is set for delivery at 11/13 @0900AM"]
    newer = {"status": "scheduled", "events": [{"evidence": {"sent": "2026-11-11T17:00:00Z"}}]}
    assert P._unmatched_for(newer, by_po[USF], now) == []
    assert P._unmatched_for({"status": "delivered"}, by_po[USF], now) == []


def test_labels_are_added_once_and_existing_categories_kept():
    cache = _cache([("a", JD, "2026-11-11T15:15:00Z", ASK_USF),
                    ("c", BELINDA, "2026-11-11T16:35:00Z", SET),
                    ("old", BELINDA, "2026-09-01T16:35:00Z", SET)])
    cache["messages"]["c"]["categories"] = ["Red category"]
    cache["messages"]["old"]["text"] = "PO 7135734C set for 9/3"
    sent = []
    fake = lambda box: (lambda m, url, data=None: sent.append((m, url, data)) or "", "BOX")
    labels, _ = B._message_plan(cache, {}, LEDGER)
    now = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)
    labeled, n, errs = B._label_mailbox(labels, cache["messages"], {}, now, call_for=fake)
    assert n == 2 and not errs
    assert ("PATCH", "BOX/gc", {"categories": ["Red category",
                                               "PO 7135734C · USF La Mirada"]}) in sent
    assert all("gold" not in u for _, u, _ in sent)          # >30 days old: left alone
    # JD removed the label in Outlook: the next sync doesn't put it back
    sent.clear()
    cache["messages"]["c"]["categories"] = ["Red category"]
    B._label_mailbox(labels, cache["messages"], labeled, now, call_for=fake)
    assert sent == []


def test_follow_ups_ask_for_the_po_number():
    body = D.followup_body([{"po_number": USF, "distributor": "US Foods",
                             "warehouse": "La Mirada, CA", "ask": "schedule"}])
    assert "Please include the PO # in your reply" in body
