"""Lineage delivery tracking: scheduled ETA -> changes -> confirmed arrival.

JD 2026-10-15: "We need to know the anticipated ETA when it's scheduled,
anytime the arrival date changes, and confirmation when the PO actually
arrives at the warehouse. Once arrival confirmation lands, the PO needs to be
updated in the Pending POs tab and flow through to the inventory tab." And:
hold a PO in Pending until Lineage confirms delivery, even past its
appointment.

Messages quoted below are real lines from the Quote and Schedule threads.
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import inventory_tracker as it
from integrations import po_delivery as D

NOW = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
ROJEAN = "rwilt@onelineage.com"
BELINDA = "bmorris@onelineage.com"

REQUEST_1012 = """Good afternoon,
We are looking to have these POs picked up from our woodside location. Please advise what size truck and how many will be sent over.
Order
Quantity: 20 pallets (1120 cases) of fresh frozen bagels
Pick-up Location: H&H Bagels, 54-18 37th Avenue, Woodside, NY 11377.
Weight: 20,000 lbs
Address: US Foods, 13400 Hollister Road, Houston, TX 77086.
Receiving Hours: 8am – 5pm PST **APPT REQUIRED**
Receiving/Scheduling Phone: 713-868-1301
PO #: 393072B2
Invoice #: 1025827
Order
Quantity: 6 pallets (336 cases) of frozen bagels
Pick-up Location: H&H Bagels, 54-18 37th Avenue, Woodside, NY 11377.
Weight: 6,000 lbs
Address: Cheney Brothers Inc, 1 Cheney Way, Riviera Beach, FL 33404.
Receiving Hours: 7am – 4pm EST **APPT REQUIRED**
Receiving/Scheduling Phone: 1-800-432-1341
PO #: 014511763291
Invoice #: 1025931
Please let me know if you have any questions.
Thanks!
-Isaiah"""

SCHEDULED_1015 = {
    "po_number": "393072B2", "event": "scheduled",
    "delivery_appt": "2026-10-19T08:00:00-05:00",
    "pickup_at": "2026-10-16T13:00:00-04:00", "confirmation_no": "18021721",
    "evidence": {"message_id": "<a@lineage>", "sender": ROJEAN,
                 "sent": "2026-10-15T14:49:57Z",
                 "quote": "Delivery moved ... Appointment: 10/19/2026 8:00 AM "
                          "Appointment Confirmation #: 18021721 PO # 393072B2"}}


def _requested(po="393072B2", sent="2026-10-12T19:16:29Z"):
    return {"po_number": po, "event": "requested",
            "thread": {"conversation_id": "c1", "subject": "Quote and Schedule - 2 Orders"},
            "evidence": {"message_id": "<req>", "sent": sent, "quote": f"PO #: {po}"}}


# ---------------------------------------------------------------------------
# H&H's request
# ---------------------------------------------------------------------------

def test_request_blocks_are_read_per_po():
    blocks = D.parse_request_blocks(REQUEST_1012)
    assert [b["po_number"] for b in blocks] == ["393072B2", "014511763291"]
    assert blocks[0]["address"] == "US Foods, 13400 Hollister Road, Houston, TX 77086"
    assert blocks[0]["quantity"].startswith("20 pallets (1120 cases)")
    assert blocks[1]["address"].startswith("Cheney Brothers Inc")
    assert blocks[1]["invoice"] == "1025931"


def test_a_follow_up_naming_one_po_still_counts():
    assert [b["po_number"] for b in
            D.parse_request_blocks("Please add this one too.\nPO #: 5903413Y\n")] == ["5903413Y"]


def test_legal_footer_is_cut_from_replies():
    t = D.clean_body("This order is all set\nRojean Wilt\nThe information contained "
                     "in this message and any accompanying documents is confidential")
    assert t.startswith("This order is all set") and "confidential" not in t


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def test_requested_scheduled_rescheduled_delivered():
    rec, ch, err = D.apply_event(None, _requested(), NOW)
    assert err is None and ch and rec["status"] == "requested"

    first = dict(SCHEDULED_1015, delivery_appt="2026-10-22T09:00:00-05:00",
                 evidence=dict(SCHEDULED_1015["evidence"], sent="2026-10-15T13:34:59Z",
                               quote="Appointment: 10/22/2026 9:00 AM"))
    rec, ch, err = D.apply_event(rec, first, NOW)
    assert err is None and rec["status"] == "scheduled"
    assert rec["delivery_appt"] == "2026-10-22T09:00:00-05:00"

    rec, ch, err = D.apply_event(rec, dict(SCHEDULED_1015, event="rescheduled"), NOW)
    assert err is None and ch["before"]["delivery_appt"] == "2026-10-22T09:00:00-05:00"
    assert rec["delivery_appt"] == "2026-10-19T08:00:00-05:00"
    assert rec["confirmation_no"] == "18021721"

    later = NOW + timedelta(days=4)
    rec, ch, err = D.apply_event(rec, {
        "po_number": "393072B2", "event": "delivered",
        "delivered_at": "2026-10-19T07:52:00-05:00",
        "evidence": {"message_id": "<d>", "sender": BELINDA, "sent": "2026-10-19T15:00:00Z",
                     "quote": "Yes, this shows delivered 0752AM/19th."}}, later)
    assert err is None and rec["status"] == "delivered"
    assert [e["event"] for e in rec["events"]] == ["requested", "scheduled",
                                                   "rescheduled", "delivered"]
    # every change keeps the line that said it
    assert rec["events"][-1]["evidence"]["quote"].startswith("Yes, this shows delivered")


def test_replay_is_a_no_op():
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    rec, _, _ = D.apply_event(rec, SCHEDULED_1015, NOW)
    again, ch, err = D.apply_event(rec, SCHEDULED_1015, NOW)
    assert ch is None and err is None and len(again["events"]) == 2


def test_an_older_message_cannot_overwrite_a_newer_appointment():
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    rec, _, _ = D.apply_event(rec, SCHEDULED_1015, NOW)          # sent 14:49
    stale = dict(SCHEDULED_1015, delivery_appt="2026-10-22T09:00:00-05:00",
                 evidence=dict(SCHEDULED_1015["evidence"], sent="2026-10-15T13:34:59Z"))
    _, ch, err = D.apply_event(rec, stale, NOW)
    assert ch is None and "stale" in err


def test_only_lineage_or_the_dock_can_move_a_date():
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    ev = dict(SCHEDULED_1015, evidence=dict(SCHEDULED_1015["evidence"],
                                            sender="isaiah@hhbagels.com"))
    _, ch, err = D.apply_event(rec, ev, NOW)
    assert ch is None and "can't report" in err
    ev = dict(SCHEDULED_1015, evidence=dict(SCHEDULED_1015["evidence"], quote=""))
    assert "evidence.quote" in D.apply_event(rec, ev, NOW)[2]
    # the receiving DC can
    ev = dict(SCHEDULED_1015, evidence=dict(SCHEDULED_1015["evidence"],
                                            sender="tom.foley@usfoods.com"))
    assert D.apply_event(rec, ev, NOW)[2] is None


def test_implausible_dates_are_refused():
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    bad = dict(SCHEDULED_1015, delivery_appt="2027-06-01T08:00:00-05:00")
    assert "implausible" in D.apply_event(rec, bad, NOW)[2]
    future = {"po_number": "393072B2", "event": "delivered",
              "delivered_at": "2026-10-19T08:00:00-05:00",
              "evidence": {"sender": BELINDA, "sent": "x", "quote": "delivered"}}
    assert "future" in D.apply_event(rec, future, NOW)[2]
    early = dict(future, delivered_at="2026-10-01T08:00:00-04:00")
    assert "before the PO" in D.apply_event(rec, early, NOW)[2]


def test_a_delivered_po_cannot_be_reopened_by_a_late_schedule_line():
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    rec, _, _ = D.apply_event(rec, {
        "po_number": "393072B2", "event": "delivered",
        "delivered_at": "2026-10-15T09:00:00-05:00",
        "evidence": {"sender": BELINDA, "sent": "2026-10-15T16:00:00Z",
                     "quote": "this shows delivered"}}, NOW)
    _, ch, err = D.apply_event(rec, SCHEDULED_1015, NOW)
    assert ch is None and "already confirmed delivered" in err


# ---------------------------------------------------------------------------
# When to follow up
# ---------------------------------------------------------------------------

def test_follow_up_rules():
    rec, _, _ = D.apply_event(None, _requested(sent="2026-10-14T19:00:00Z"), NOW)
    assert D.chase_due(rec, NOW) is None                         # 23 h in
    assert D.chase_due(rec, NOW + timedelta(hours=22)) == "schedule"

    rec, _, _ = D.apply_event(rec, SCHEDULED_1015, NOW)
    appt = datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc)   # 8:00 CT
    assert D.chase_due(rec, appt + timedelta(hours=2)) is None
    assert D.awaiting_confirmation(rec, appt + timedelta(hours=2))
    t = appt + timedelta(hours=3)
    assert D.chase_due(rec, t) == "confirm_delivery"
    rec = D.record_chase(rec, "confirm_delivery", t)
    assert D.chase_due(rec, t + timedelta(hours=6)) is None     # not twice a day
    assert D.chase_due(rec, t + timedelta(hours=18)) == "confirm_delivery"


def test_summary_shows_the_docks_wall_clock():
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    rec, _, _ = D.apply_event(rec, SCHEDULED_1015, NOW)
    s = D.summary(rec, NOW)
    assert s["delivery_appt_local"] == "2026-10-19T08:00:00"
    assert s["confirmation_no"] == "18021721" and not s["awaiting_confirmation"]


# ---------------------------------------------------------------------------
# The rollover holds until confirmed
# ---------------------------------------------------------------------------

KEY = "plain bagel 4oz [usf - houston]"


def _inv(eta="2026-10-10T00:00:00", source=""):
    entry = {"po_number": "393072B2", "qty": 224.0, "unit": "cs",
             "ordered_at": "2026-09-28T00:00:00", "eta": eta, "lead_days": 30}
    if source:
        entry["arrival_source"] = source
        entry["arrival_date"] = eta
    return {KEY: {"name": "Plain Bagel 4oz [USF - Houston]", "quantity": 72.0,
                  "unit": "cs", "warehouse": "Houston, TX",
                  "last_count_at": "2026-10-13T14:26:49Z", "on_order": [entry]}}


def _roll(monkeypatch, inv, tracking):
    monkeypatch.setattr(it, "load_po_delivery", lambda: tracking)
    it._rollover_on_order(inv)
    return inv[KEY]


def test_untracked_po_rolls_over_at_its_eta_as_before(monkeypatch):
    item = _roll(monkeypatch, _inv(eta="2026-10-14T00:00:00"), {})
    assert item["quantity"] == 296.0 and item["on_order"] == []


def test_tracked_po_is_held_past_its_date_until_confirmed(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    item = _roll(monkeypatch, _inv(), {"393072B2": rec})
    assert item["quantity"] == 72.0 and len(item["on_order"]) == 1

    rec, _, _ = D.apply_event(rec, dict(SCHEDULED_1015,
                                         delivery_appt="2026-10-14T08:00:00-05:00",
                                         pickup_at=""), NOW)
    item = _roll(monkeypatch, _inv(eta="2026-10-14T08:00:00", source="lineage"),
                 {"393072B2": rec})
    assert item["quantity"] == 72.0, "appointment passed, nobody confirmed: hold"


def test_confirmed_delivery_rolls_in_dated_to_the_delivery(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    rec, _, _ = D.apply_event(rec, {
        "po_number": "393072B2", "event": "delivered",
        "delivered_at": "2026-10-15T07:52:00-05:00",
        "evidence": {"sender": BELINDA, "sent": "2026-10-15T16:00:00Z",
                     "quote": "this shows delivered 0752AM/15th"}}, NOW)
    item = _roll(monkeypatch, _inv(eta="2026-10-28T00:00:00"), {"393072B2": rec})
    assert item["quantity"] == 296.0 and item["on_order"] == []
    audit = it._rollover_stage()["audit"][-1]
    assert audit["eta"].startswith("2026-10-15")


def test_delivery_inside_a_later_count_is_absorbed_not_added(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(sent="2026-10-01T12:00:00Z"), NOW)
    rec, _, _ = D.apply_event(rec, {
        "po_number": "393072B2", "event": "delivered",
        "delivered_at": "2026-10-07T07:00:00-04:00",
        "evidence": {"sender": BELINDA, "sent": "2026-10-08T13:55:08Z",
                     "quote": "Yes, this shows delivered 10/07."}}, NOW)
    item = _roll(monkeypatch, _inv(eta="2026-10-28T00:00:00"), {"393072B2": rec})
    # the 10/13 count already includes the 10/07 delivery
    assert item["quantity"] == 72.0 and item["on_order"] == []


def test_an_operators_explicit_arrival_overrides_the_hold(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    item = _roll(monkeypatch, _inv(eta="2026-10-14T00:00:00", source="operator"),
                 {"393072B2": rec})
    assert item["quantity"] == 296.0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

ROJEAN_1015 = ("Delivery moved to Monday.\nAppointment: 10/19/2026 8:00 AM\n"
               "Appointment Confirmation #: 18021721\nPO # 393072B2")


def _synced(messages=None, synced_h=0.1, last_h=6.0, errors=None,
            pos=("393072B2",), reply_id="r1-jd", base=True):
    """A sync-threads cache as the last read left it."""
    msgs = {"<a@lineage>": {"from": ROJEAN, "sent": "2026-10-15T14:49:57Z",
                            "conversation_id": "conv1", "text": ROJEAN_1015},
            "<d@lineage>": {"from": ROJEAN, "sent": "2026-10-15T16:31:23Z",
                            "conversation_id": "conv1",
                            "text": "Delivered 0752AM/15th"}} if base else {}
    msgs.update(messages or {})
    return {"synced_at": (NOW - timedelta(hours=synced_h)).isoformat(),
            "errors": list(errors or []), "messages": msgs,
            "threads": {"conv1": {
                "conversation_id": "conv1", "subject": "Quote and Schedule - 2 Orders",
                "reply_to_graph_id": reply_id,
                "reply_subject": "RE: Quote and Schedule - 2 Orders",
                "last_sent": (NOW - timedelta(hours=last_h)).isoformat(),
                "last_from": ROJEAN, "po_numbers": list(pos)}}}


def _routes(monkeypatch, inv, tracking, ledger_status="in_transit", synced=None,
            extra_ledger=None):
    import blueprints.delivery as B
    from app import app
    store = {"tracking": tracking, "inv": inv,
             "threads": synced if synced is not None else _synced(),
             "sent": []}
    monkeypatch.setattr(it, "load_po_delivery", lambda: store["tracking"])
    monkeypatch.setattr(it, "save_po_delivery", lambda d: store.update(tracking=d))
    monkeypatch.setattr(it, "load_po_delivery_threads", lambda: store["threads"])
    monkeypatch.setattr(it, "save_po_delivery_threads", lambda d: store.update(threads=d))
    monkeypatch.setattr(it, "load_inventory", lambda: (it._rollover_on_order(store["inv"]), store["inv"])[1])
    monkeypatch.setattr(it, "save_inventory", lambda d: store.update(inv=d))
    ledger = {"393072B2": {"po_number": "393072B2", "status": ledger_status,
                           "distributor": "US Foods", "warehouse": "Houston, TX",
                           "total_cs": 1120.0}}
    ledger.update(extra_ledger or {})
    monkeypatch.setattr(B, "_ledger_by_po", lambda: ledger)
    monkeypatch.setattr(B, "_now", lambda: NOW)

    drafts = {}

    def create(sender, reply_to):
        draft_id = f"draft-{len(drafts) + 1}"
        drafts[draft_id] = {"from": sender, "reply_to": reply_to}
        return draft_id

    def send(sender, draft_id, to, cc, subject, body):
        store["sent"].append({**drafts[draft_id], "to": list(to), "cc": list(cc),
                              "subject": subject, "body": body})
    monkeypatch.setattr(B, "_create_reply", create)
    monkeypatch.setattr(B, "_send_draft", send)
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    monkeypatch.setenv("DELIVERY_API_TOKEN", "dtok")
    return app.test_client(), store


def test_events_route_moves_dates_and_releases_on_delivery(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    c, store = _routes(monkeypatch, _inv(eta="2026-10-28T00:00:00"), {"393072B2": rec})
    h = {"X-Inventory-Token": "tok"}
    out = c.post("/api/pos/delivery/events", json={"events": [SCHEDULED_1015]},
                 headers=h).get_json()
    assert out["ok"] and out["results"][0]["applied"], out
    e = store["inv"][KEY]["on_order"][0]
    assert e["arrival_date"] == "2026-10-19T08:00:00" and e["arrival_source"] == "lineage"
    assert e["ship_date"] == "2026-10-16T13:00:00"

    bad = c.post("/api/pos/delivery/events", json={"events": [
        {"po_number": "NOPE123", "event": "delivered"}]}, headers=h).get_json()
    assert bad["results"][0]["reason"] == "PO not in the tracker"

    delivered = {"po_number": "393072B2", "event": "delivered",
                 "delivered_at": "2026-10-15T07:52:00-05:00",
                 "evidence": {"message_id": "<d@lineage>", "sender": ROJEAN,
                              "sent": "2026-10-15T16:31:23Z",
                              "quote": "Delivered 0752AM/15th"}}
    out = c.post("/api/pos/delivery/events", json={"events": [delivered]},
                 headers=h).get_json()
    assert out["results"][0]["applied"], out
    assert store["inv"][KEY]["quantity"] == 296.0 and store["inv"][KEY]["on_order"] == []


def test_chased_route_records_the_follow_up(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    c, store = _routes(monkeypatch, _inv(), {"393072B2": rec})
    out = c.post("/api/pos/delivery/chased", json={
        "po_numbers": ["393072B2", "X1"], "ask": "schedule", "message_id": "m"},
        headers={"X-Inventory-Token": "tok"}).get_json()
    assert out["recorded"] == ["393072B2"] and out["not_tracked"] == ["X1"]
    assert store["tracking"]["393072B2"]["chases"][0]["ask"] == "schedule"


def test_sync_threads_registers_open_pos_and_returns_jds_reply_id(monkeypatch):
    import blueprints.delivery as B
    import blueprints.email as E
    c, store = _routes(monkeypatch, _inv(eta="2026-10-10T00:00:00"), {})
    monkeypatch.setenv("MS365_USER", "JD@ms.hhbagels.com,info@ms.hhbagels.com")
    monkeypatch.setattr(E, "_graph_token", lambda: "g")
    iso = lambda h: (NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def msg(mid, h, sender, text, mailbox_tag=""):
        return {"id": mid + mailbox_tag, "subject": "RE: Quote and Schedule - 2 Orders",
                "conversationId": "conv1", "sentDateTime": iso(h),
                "internetMessageId": f"<{mid}>",
                "from": {"emailAddress": {"address": sender}},
                "toRecipients": [], "ccRecipients": [],
                "uniqueBody": {"content": text}}

    jd = [msg("req", 70, "Isaiah@hhbagels.com", REQUEST_1012, "-jd"),
          msg("r1", 2, ROJEAN, "This order is all set\nThe information contained in this message", "-jd")]
    info = [msg("r1", 2, ROJEAN, "This order is all set", "-info")]

    def graph(url, token, extra=None, timeout=30):
        return {"value": jd if "/users/JD%40" in url else info}
    monkeypatch.setattr(E, "_graph_json", graph)
    out = c.post("/api/pos/delivery/sync-threads", json={},
                 headers={"X-Inventory-Token": "tok"}).get_json()
    assert out["ok"], out
    assert out["registered"] == ["393072B2"]
    assert out["not_in_tracker"] == ["014511763291"]
    t = out["threads"][0]
    assert t["reply_to_graph_id"] == "r1-jd"
    assert [p["po_number"] for p in t["open_pos"]] == ["393072B2"]
    assert t["open_pos"][0]["destination"].startswith("US Foods, 13400 Hollister")
    assert t["messages"][-1]["text"] == "This order is all set"
    assert store["tracking"]["393072B2"]["status"] == "requested"
    # what it read is cached for the quote check and the follow-ups
    th = store["threads"]["threads"]["conv1"]
    assert th["po_numbers"] == ["393072B2", "014511763291"]
    assert th["reply_to_graph_id"] == "r1-jd" and th["last_from"] == ROJEAN
    assert store["threads"]["messages"]["<r1>"]["text"] == "This order is all set"
    assert store["threads"]["synced_at"] == NOW.isoformat()
    # and the hold is now on: its 10/10 ETA has passed, it stays pending
    it.load_inventory()
    assert store["inv"][KEY]["quantity"] == 72.0
    assert len(store["inv"][KEY]["on_order"]) == 1


# ---------------------------------------------------------------------------
# Pending POs tab (the ledger)
# ---------------------------------------------------------------------------

def _ledger(monkeypatch, inv, tracking):
    import blueprints.pos as P
    monkeypatch.setattr(it, "load_inventory", lambda: inv)
    monkeypatch.setattr(it, "load_usage", lambda: [])
    monkeypatch.setattr(it, "load_chefs_warehouse_pos", lambda: [])
    monkeypatch.setattr(it, "load_canceled_pos", lambda: {})
    monkeypatch.setattr(it, "load_status_overrides", lambda: {})
    monkeypatch.setattr(it, "load_po_delivery", lambda: tracking)
    monkeypatch.setattr(P, "_freight_ship_date_index", lambda: {})
    return {r["po_number"]: r for r in P.build_po_ledger()}


def test_ledger_shows_the_appointment_and_never_calls_a_held_po_arrived(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    rec, _, _ = D.apply_event(rec, dict(SCHEDULED_1015,
                                         delivery_appt="2026-10-14T08:00:00-05:00"), NOW)
    inv = _inv(eta="2026-10-14T08:00:00", source="lineage")
    r = _ledger(monkeypatch, inv, {"393072B2": rec})["393072B2"]
    assert r["status"] != "arrived", "appointment passed but unconfirmed"
    assert r["arrival_date"] == "2026-10-14T08:00:00"
    assert r["ship_date"] == "2026-10-16T13:00:00" and r["ship_date_source"] == "lineage"
    assert r["delivery"]["awaiting_confirmation"] is True
    assert r["delivery"]["confirmation_no"] == "18021721"

    # untracked, the same past date reads as arrived (unchanged behaviour)
    r = _ledger(monkeypatch, _inv(eta="2026-10-14T08:00:00", source="freight"),
                {})["393072B2"]
    assert r["status"] == "arrived" and "delivery" not in r


def test_blocks_without_a_quantity_label_keep_their_own_address():
    """10/02 '6 Orders': the Bronx blocks say '2 pallets (112 cases) ...' with
    no 'Quantity:' label; each PO must still get its own destination."""
    text = ("Order\nQuantity: 4 pallets (224 cases) of fresh frozen bagels\n"
            "Address: Chef’s Warehouse, 2801 South Western Avenue, Chicago, IL. 60608.\n"
            "PO #: 1209157\nInvoice #: 1025806\nOrder\n"
            "2 pallets (112 cases) of fresh frozen bagels\nWeight: 2,000 lbs\n"
            "Address: Dairyland USA Corporation, 240 Food Center Drive, Bronx, NY. 10474\n"
            "PO #: 1209205\nInvoice #: 1025807\n")
    b = D.parse_request_blocks(text)
    assert [x["po_number"] for x in b] == ["1209157", "1209205"]
    assert b[0]["address"].startswith("Chef’s Warehouse, 2801 South Western")
    assert b[1]["address"].startswith("Dairyland USA")
    assert b[1]["quantity"].startswith("2 pallets (112 cases)")
    assert [x["invoice"] for x in b] == ["1025806", "1025807"]


# ---------------------------------------------------------------------------
# No laptop (10/15): limited key, evidence checked, server-sent follow-ups
# ---------------------------------------------------------------------------

def test_a_date_only_moves_on_a_line_the_mailbox_really_holds(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    c, store = _routes(monkeypatch, _inv(eta="2026-10-28T00:00:00"), {"393072B2": rec},
                       synced=_synced(messages={
                           "<hh@x>": {"from": "isaiah@hhbagels.com",
                                      "sent": "2026-10-15T15:00:00Z",
                                      "conversation_id": "conv1",
                                      "text": "Lineage says delivered 0752AM/15th"},
                           "<other@lineage>": {"from": ROJEAN, "sent": "2026-10-15T15:00:00Z",
                                               "conversation_id": "conv9",
                                               "text": "Delivered 0752AM/15th"}}))
    h = {"X-Inventory-Token": "tok"}

    def post(**evid):
        ev = {"po_number": "393072B2", "event": "delivered",
              "delivered_at": "2026-10-15T07:52:00-05:00",
              "evidence": {"sender": ROJEAN, "sent": "2026-10-15T16:31:23Z", **evid}}
        return c.post("/api/pos/delivery/events", json={"events": [ev]},
                      headers=h).get_json()["results"][0]

    assert "isn't a message the last sync read" in post(
        message_id="<made-up>", quote="Delivered 0752AM/15th")["reason"]
    assert "isn't in that message" in post(
        message_id="<d@lineage>", quote="Delivered 0700AM/15th")["reason"]
    assert "doesn't list this PO" in post(
        message_id="<other@lineage>", quote="Delivered 0752AM/15th")["reason"]
    # the sender is the mailbox's, not the caller's: H&H can't confirm a delivery
    assert "can't report a delivery date" in post(
        message_id="<hh@x>", quote="says delivered 0752AM/15th")["reason"]
    assert store["inv"][KEY]["on_order"], "nothing released"
    # line breaks / ellipsis in the quote are fine
    ok = c.post("/api/pos/delivery/events", json={"events": [SCHEDULED_1015]},
                headers=h).get_json()["results"][0]
    assert ok["applied"], ok
    assert store["tracking"]["393072B2"]["events"][-1]["evidence"]["sender"] == ROJEAN


def test_the_delivery_key_opens_only_the_delivery_routes(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(), NOW)
    c, store = _routes(monkeypatch, _inv(), {"393072B2": rec})
    d = {"X-Inventory-Token": "dtok"}
    assert c.get("/api/pos/delivery", headers=d).get_json()["ok"]
    assert c.get("/api/pos/ledger", headers=d).status_code == 401
    assert c.post("/api/email/send", json={"to": "x@y.com"}, headers=d).status_code == 401
    assert c.post("/api/email/ingest-events", json={}, headers=d).status_code == 401
    assert c.post("/api/pos/delivery/chased", json={"po_numbers": ["393072B2"],
                                                    "ask": "schedule"},
                  headers=d).status_code == 401
    r = c.post("/api/pos/delivery/events", json={"events": [
        {"po_number": "393072B2", "event": "released"}]}, headers=d).get_json()
    assert "isn't allowed with the delivery key" in r["results"][0]["reason"]
    assert store["tracking"]["393072B2"]["status"] == "requested"
    r = c.post("/api/pos/delivery/events", json={"events": [SCHEDULED_1015]},
               headers=d).get_json()
    assert r["results"][0]["applied"], r
    assert c.get("/api/pos/delivery", headers={"X-Inventory-Token": "nope"}).status_code == 401


def test_server_sends_one_threaded_follow_up_per_thread_and_records_it(monkeypatch):
    houston, _, _ = D.apply_event(None, _requested(sent="2026-10-12T19:16:29Z"), NOW)
    houston, _, _ = D.apply_event(houston, dict(
        SCHEDULED_1015, delivery_appt="2026-10-15T08:00:00-05:00"), NOW)
    cheney, _, _ = D.apply_event(None, _requested("014511763291",
                                                  sent="2026-10-12T19:16:29Z"), NOW)
    c, store = _routes(
        monkeypatch, _inv(), {"393072B2": houston, "014511763291": cheney},
        synced=_synced(pos=("393072B2", "014511763291"), base=False),
        extra_ledger={"014511763291": {"po_number": "014511763291",
                                       "status": "in_transit",
                                       "distributor": "Cheney Brothers",
                                       "warehouse": "Riviera Beach, FL"}})
    d = {"X-Inventory-Token": "dtok"}
    dry = c.post("/api/pos/delivery/send-followups", json={"dry_run": True},
                 headers=d).get_json()
    assert dry["ok"] and dry["emails"][0]["would_send"] and store["sent"] == []

    out = c.post("/api/pos/delivery/send-followups", json={}, headers=d).get_json()
    assert out["ok"] and out["sent"] == 1, out
    [mail] = store["sent"]
    assert mail["from"] == "jd@hhbagels.com" and mail["reply_to"] == "r1-jd"
    assert mail["to"] == list(D.FOLLOWUP_TO) and mail["cc"] == list(D.FOLLOWUP_CC)
    assert mail["subject"] == "RE: Quote and Schedule - 2 Orders"
    assert ("- PO 393072B2 (US Foods, Houston, TX): can you confirm it delivered at "
            "its 10/15 8:00 AM CT appointment?") in mail["body"]
    assert ("- PO 014511763291 (Cheney Brothers, Riviera Beach, FL): could you send "
            "the pickup date and the delivery appointment") in mail["body"]
    assert mail["body"].rstrip().endswith("New York, NY 10001")
    asks = {k: [x["ask"] for x in v["chases"]] for k, v in store["tracking"].items()}
    assert asks == {"393072B2": ["confirm_delivery"], "014511763291": ["schedule"]}

    again = c.post("/api/pos/delivery/send-followups", json={}, headers=d).get_json()
    assert again["sent"] == 0 and again["emails"] == [], "never twice within 16 h"


def test_follow_ups_wait_for_a_fresh_full_read_and_a_quiet_po(monkeypatch):
    rec, _, _ = D.apply_event(None, _requested(sent="2026-10-12T19:16:29Z"), NOW)
    for synced, why in ((_synced(synced_h=2, base=False), "not synced"),
                        (_synced(errors=["info@: list: boom"], base=False), "partial read")):
        c, store = _routes(monkeypatch, _inv(), {"393072B2": dict(rec)}, synced=synced)
        out = c.post("/api/pos/delivery/send-followups", json={},
                     headers={"X-Inventory-Token": "dtok"}).get_json()
        assert not out["ok"] and why in out["error"] and store["sent"] == []

    def recent(text, h=1, sender=ROJEAN):
        return {"<n@x>": {"from": sender, "conversation_id": "conv1", "text": text,
                          "sent": (NOW - timedelta(hours=h)).isoformat()}}
    cheney, _, _ = D.apply_event(None, _requested("014511763291",
                                                  sent="2026-10-12T19:16:29Z"), NOW)
    cheney_ledger = {"014511763291": {"po_number": "014511763291", "status": "in_transit",
                                      "distributor": "Cheney Brothers",
                                      "warehouse": "Riviera Beach, FL"}}

    def run(msgs):
        c, store = _routes(monkeypatch, _inv(),
                           {"393072B2": dict(rec), "014511763291": dict(cheney)},
                           synced=_synced(msgs, pos=("393072B2", "014511763291"),
                                          base=False),
                           extra_ledger=cheney_ledger)
        out = c.post("/api/pos/delivery/send-followups", json={},
                     headers={"X-Inventory-Token": "dtok"}).get_json()
        return out, store

    # a reply naming no PO ("This order is all set") holds the whole thread
    out, store = run(recent("This order is all set"))
    assert out["sent"] == 0 and "giving them time" in out["emails"][0]["skipped"]
    assert store["sent"] == [] and store["tracking"]["393072B2"]["chases"] == []
    # a reply about one PO holds only that one
    out, store = run(recent("Checking on PO 393072B2, will advise"))
    assert out["sent"] == 1 and out["emails"][0]["po_numbers"] == ["014511763291"]
    assert list(out["emails"][0]["held_back"]) == ["393072B2"]
    assert "393072B2" not in store["sent"][0]["body"]
    assert store["tracking"]["393072B2"]["chases"] == []
    # older than 4 h, or an internal test reply: not activity
    for msgs in (recent("This order is all set", h=5),
                 recent("TEST (internal, ignore): threading check", sender="jd@hhbagels.com")):
        out, store = run(msgs)
        assert out["sent"] == 1 and out["emails"][0]["po_numbers"] == ["014511763291",
                                                                      "393072B2"]


def test_appt_label_reads_the_zone_off_the_offset():
    assert D.appt_label("2026-10-19T08:00:00-05:00") == "10/19 8:00 AM CT"
    assert D.appt_label("2026-10-21T07:00:00-04:00") == "10/21 7:00 AM ET"
    assert D.appt_label("2026-12-02T13:30:00-05:00") == "12/2 1:30 PM ET"   # EST
    assert D.appt_label("2026-10-20T09:00:00-07:00") == "10/20 9:00 AM PT"
    assert D.quote_in_text("Delivery moved ... Appointment: 10/19/2026 8:00 AM",
                           ROJEAN_1015)
    assert not D.quote_in_text("Appointment: 10/19/2026 8:00 AM ... Delivery moved",
                               ROJEAN_1015), "pieces must appear in order"
    assert not D.quote_in_text("...", ROJEAN_1015)
