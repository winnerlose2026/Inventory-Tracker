"""Third-party carriers (Bozel, FFE) can confirm a delivery (JD 11/17: "treat
that confirmation from third-party carriers as the same as Lineage, but still
get final confirmation from Lineage too").

A carrier's "delivered" marks the PO arrived right away -- provisional
(delivery_final False) -- and the follow-ups then ask Lineage to confirm.
Lineage / the DC's answer makes it final, and can correct the date.
"""
import sys
from datetime import timedelta

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from integrations import po_delivery as D
from test_po_delivery import BELINDA, NOW, REQUEST_1012
from test_delivery_forked_threads import BASE, _msg, _run
from test_delivery_server_confirm import _scheduled

BOZEL = "mbozel@bozelt.com"
FFE = "sstrimple@ffex.net"
BOZEL_SUBJ = "RE: Bozel pickup request - PO 393072B2 ETA"


def _req():
    return _msg("req", 70, "Isaiah@hhbagels.com", REQUEST_1012, BASE, "conv1")


def test_carrier_delivered_marks_arrived_but_not_final(monkeypatch):
    msgs = [_req(), _msg("bz", 6, BOZEL, "This load delivered 10/15\n\nMoe",
                         BOZEL_SUBJ, "convB")]
    out, store, _q = _run(monkeypatch, msgs)
    rec = store["tracking"]["393072B2"]
    assert rec["status"] == "delivered" and rec["delivered_at"].startswith("2026-10-15")
    assert rec["delivery_final"] is False and rec["delivery_confirmed_by"] == "Bozel"
    assert [g["po_number"] for g in out["confirmed_by_server"] if g["applied"]] == ["393072B2"]
    # on-hand moves now: the rollover gate releases the held PO
    import inventory_tracker as it
    from test_po_delivery import KEY
    assert it.load_inventory()[KEY]["on_order"] == []
    s = D.summary(rec, NOW)
    assert s["awaiting_final"] and not s["delivery_final"]
    assert D.chase_due(rec, NOW) == "confirm_final"


def test_lineage_after_carrier_makes_it_final_without_moving_the_date(monkeypatch):
    msgs = [_req(),
            _msg("bz", 6, BOZEL, "This load delivered 10/15\n\nMoe", BOZEL_SUBJ, "convB"),
            _msg("bm", 1, BELINDA, "Noted.\n393072B2-delivered\nBelinda Morris",
                 BASE, "conv1")]
    out, store, _q = _run(monkeypatch, msgs)
    rec = store["tracking"]["393072B2"]
    # Lineage's word is taken first when both are there
    assert rec["status"] == "delivered" and rec.get("delivery_final") is True
    assert rec["delivery_confirmed_by"] == "Lineage"
    assert rec["delivered_at"].startswith("2026-10-15")
    assert D.chase_due(rec, NOW) is None


def test_lineage_finalizes_a_carrier_record_later_and_corrects_the_date():
    rec = _scheduled()
    ev = lambda sender, mid, at, quote, **kw: {  # noqa: E731
        "po_number": "393072B2", "event": "delivered", "delivered_at": at,
        "evidence": {"message_id": mid, "sender": sender,
                     "sent": "2026-10-15T17:00:00Z", "quote": quote}, **kw}
    rec, ch, err = D.apply_event(rec, ev(BOZEL, "a", "2026-10-15", "delivered 10/15"), NOW)
    assert not err and ch and rec["delivery_final"] is False
    # a second carrier note can't move it
    _r, ch2, err2 = D.apply_event(rec, ev(FFE, "b", "2026-10-14", "delivered 10/14"), NOW)
    assert err2 and ch2 is None
    # Lineage, undated: final, date kept
    r2, ch3, err3 = D.apply_event(rec, ev(BELINDA, "c", "2026-10-15",
                                          "393072B2-delivered", date_estimated=True), NOW)
    assert not err3 and ch3 and r2["delivery_final"] is True
    assert r2["delivered_at"] == "2026-10-15"
    # Lineage, dated differently: final and corrected
    r3, ch4, err4 = D.apply_event(rec, ev(BELINDA, "d", "2026-10-14",
                                          "This load delivered 10/14"), NOW)
    assert not err4 and r3["delivery_final"] is True and r3["delivered_at"] == "2026-10-14"
    # after Lineage, a carrier can't change it
    _r, ch5, err5 = D.apply_event(r3, ev(BOZEL, "e", "2026-10-15", "delivered 10/15"), NOW)
    assert err5 and ch5 is None


def test_unknown_senders_still_cannot_confirm(monkeypatch):
    msgs = [_req(), _msg("x", 6, "driver@gmail.com", "This load delivered 10/15",
                         BOZEL_SUBJ, "convB")]
    _out, store, _q = _run(monkeypatch, msgs)
    assert store["tracking"]["393072B2"]["status"] == "scheduled"


def test_carrier_confirmation_found_by_po_in_the_body(monkeypatch):
    # FFE's thread subject never names the PO
    msgs = [_req(), _msg("ffe", 6, FFE, "PO 393072B2\nThis load delivered 10/15\n\nStacy",
                         "Re: Friday FFE Pick up appointment", "convF")]
    _out, store, queries = _run(monkeypatch, msgs)
    assert any("393072B2" in q and "subject%3A393072B2" not in q for q in queries)
    rec = store["tracking"]["393072B2"]
    assert rec["status"] == "delivered" and rec["delivery_confirmed_by"] == "FFE"


def test_followup_asks_lineage_for_the_final_word(monkeypatch):
    msgs = [_req(), _msg("bz", 6, BOZEL, "This load delivered 10/15\n\nMoe",
                         BOZEL_SUBJ, "convB")]
    _out, store, _q = _run(monkeypatch, msgs)
    import blueprints.delivery as B
    from app import app
    out = app.test_client().post("/api/pos/delivery/send-followups", json={},
                                 headers={"X-Inventory-Token": "tok"}).get_json()
    assert out["ok"], out
    [email] = [e for e in out["emails"] if e.get("po_numbers")]
    assert email["conversation_id"] == "conv1"          # the Lineage thread, not Bozel's
    assert email["asks"]["393072B2"] == "confirm_final"
    assert "Bozel reports it delivered 10/15" in store["sent"][0]["body"]


def test_old_delivered_records_count_as_final():
    rec = {**_scheduled(), "status": "delivered", "delivered_at": "2026-10-15"}
    assert not D.awaiting_final(rec) and D.chase_due(rec, NOW) is None
