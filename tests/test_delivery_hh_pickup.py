"""H&H may set the planned Woodside pickup (JD 11/19), never a delivery date."""
from datetime import datetime, timezone
from integrations.po_delivery import apply_event, new_record

NOW = datetime(2026, 11, 19, 19, 40, tzinfo=timezone.utc)


def _rec():
    r = new_record("014511788250")
    r["status"] = "scheduled"
    r["pickup_at"] = "2026-11-20T15:00:00-05:00"
    r["pickup_evidence_sent"] = "2026-11-18T21:06:48Z"
    return r


def _ev(kind, sender, **kw):
    return {"po_number": "014511788250", "event": kind,
            "evidence": {"message_id": "<m>", "sender": sender,
                         "sent": "2026-11-19T19:38:11Z", "quote": "loading today"}, **kw}


def test_hh_can_move_pickup():
    rec, change, err = apply_event(_rec(), _ev("pickup_scheduled", "JD@hhbagels.com",
                                               pickup_at="2026-11-19T15:00:00-05:00"), NOW)
    assert err is None and change is not None
    assert rec["pickup_at"] == "2026-11-19T15:00:00-05:00"


def test_hh_cannot_set_delivery_appt():
    _, _, err = apply_event(_rec(), _ev("scheduled", "JD@hhbagels.com",
                                        delivery_appt="2026-12-01T09:00:00-05:00"), NOW)
    assert err and "can't report" in err


def test_other_domain_still_refused():
    _, _, err = apply_event(_rec(), _ev("pickup_scheduled", "x@gmail.com",
                                        pickup_at="2026-11-19T15:00:00-05:00"), NOW)
    assert err and "can't report" in err
