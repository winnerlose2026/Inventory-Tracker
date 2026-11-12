"""CSV attachments and "# event:" body lines are applied only from an internal
or known-distributor sender; To/Cc qualifies a message only when we sent it
(review 2026-11-12, item 3)."""
from integrations import email_scanner as ES

from ingest_fakes import CSV, mime

CSV_BYTES = b"variety,warehouse,quantity\nPlain,\"Ocala, FL\",480\n"
BODY = "# event: on_hand\nPlain @ Ocala, FL: 480 each\n"


def _parse(sender, **kw):
    return ES.parse_message_with_errors(mime(sender, "numbers", **kw))


def test_csv_from_a_stranger_is_ignored_with_an_error():
    events, errors, _ = _parse("someone@gmail.com",
                               attachments=[("on_hand.csv", CSV_BYTES, CSV)])
    assert events == []
    assert any("on_hand.csv" in e and "ignored" in e for e in errors)


def test_body_event_lines_from_a_stranger_are_ignored():
    events, _errors, _ = _parse("someone@gmail.com", text=BODY)
    assert events == []


def test_internal_and_distributor_senders_still_work():
    for sender in ("jd@hhbagels.com", "info@ms.hhbagels.com",
                   "mross@cheneybrothers.com", "maria.hernandez@usfoods.com"):
        events, _e, _ = _parse(sender, attachments=[("on_hand.csv", CSV_BYTES, CSV)])
        assert len(events) == 1 and events[0].event_type == "on_hand", sender
        events, _e, _ = _parse(sender, text=BODY)
        assert len(events) == 1, sender


def test_display_name_spoof_is_not_internal():
    events, _e, _ = _parse('"jd@hhbagels.com" <x@evil.example>', text=BODY)
    assert events == []


def _listing(sender, to=(), cc=()):
    return {"from": {"emailAddress": {"address": sender}},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in cc]}


def test_candidate_prefilter_uses_to_cc_only_for_internal_senders():
    rep = "maria.hernandez@usfoods.com"
    assert ES._msg_event_candidate(_listing(rep, to=["jd@hhbagels.com"]))
    assert ES._msg_event_candidate(_listing("jd@hhbagels.com", to=[rep]))
    assert ES._msg_event_candidate(_listing("info@ms.hhbagels.com", cc=[rep]))
    # A stranger addressing or cc'ing a rep does not qualify any more.
    assert not ES._msg_event_candidate(_listing("x@evil.example", to=[rep]))
    assert not ES._msg_event_candidate(
        _listing("x@evil.example", to=["jd@hhbagels.com"], cc=["po@usfoods.com"]))
