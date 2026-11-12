"""The scan covers every folder except Sent / Drafts / Deleted / Junk
(review 2026-11-12, item 6)."""
from urllib.parse import parse_qs, urlparse

from integrations import email_scanner as ES

from ingest_fakes import FakeGraph, mime, scan_with

REP = "maria.hernandez@usfoods.com"
TABLE = ("<table><tr><td>ITEM</td><td>Vendor#</td><td>CURRENT ON HAND</td></tr>"
         "<tr><td>7095637</td><td>1150</td><td>{q}</td></tr></table>")


def _m(mid, folder, qty, sender=REP):
    return {"id": mid, "sender": sender, "subject": "Weekly numbers",
            "folder": folder,
            "mime": mime(sender, "Weekly numbers", html=TABLE.format(q=qty),
                         msgid=f"<{mid}@x>")}


def test_all_folders_listed_and_excluded_folders_skipped(monkeypatch):
    monkeypatch.delenv("MS365_FOLDER", raising=False)
    monkeypatch.delenv("MS365_SCAN_ALL_FOLDERS", raising=False)
    g = FakeGraph([_m("a", "inbox-id", 11), _m("b", "reports-subfolder-id", 22),
                   _m("c", "sentitems-id", 33), _m("d", "deleteditems-id", 44),
                   _m("e", "drafts-id", 55), _m("f", "junkemail-id", 66)])
    res = scan_with(monkeypatch, g)
    assert sorted(e.item.quantity for e in res.events) == [11.0, 22.0]
    assert res.messages_seen == 2
    lists = [u for u in g.calls if "/messages?" in u]
    assert lists and all("/mailFolders/" not in u for u in lists)
    for u in lists:   # hasAttachments in a $filter 504s on these mailboxes
        filt = parse_qs(urlparse(u).query).get("$filter", [""])[0]
        assert filt.startswith("receivedDateTime ge") and "hasAttachments" not in filt
    # The rep's message in Junk is flagged for a person, never applied.
    assert [i["key"] for i in res.issues] == ["junk-mail:<f@x>"]
    assert any("Junk Email" in e for e in res.errors)


def test_explicit_folder_and_off_switch_scan_one_folder(monkeypatch):
    msgs = [_m("a", "inbox-id", 11), _m("c", "sentitems-id", 33)]
    monkeypatch.setenv("MS365_FOLDER", "Distributors")
    g = FakeGraph(msgs)
    scan_with(monkeypatch, g)
    assert any("/mailFolders/Distributors/messages?" in u for u in g.calls)
    assert not any(u.split("?")[0].endswith("/mailFolders/sentitems") for u in g.calls)

    monkeypatch.setenv("MS365_FOLDER", "Inbox")
    monkeypatch.setenv("MS365_SCAN_ALL_FOLDERS", "off")
    g = FakeGraph(msgs)
    scan_with(monkeypatch, g)
    assert any("/mailFolders/Inbox/messages?" in u for u in g.calls)


def test_unresolvable_sent_folder_falls_back_to_inbox_only(monkeypatch):
    monkeypatch.delenv("MS365_FOLDER", raising=False)
    monkeypatch.delenv("MS365_SCAN_ALL_FOLDERS", raising=False)
    g = FakeGraph([_m("a", "inbox-id", 11)],
                  fail={"/mailFolders/sentitems": [RuntimeError("403")]})
    res = scan_with(monkeypatch, g)
    assert any("/mailFolders/Inbox/messages?" in u for u in g.calls)
    assert any("scanned the Inbox only" in e for e in res.errors)
    assert not any(ES.is_mailbox_failure(e) for e in res.errors)
    assert len(res.events) == 1
