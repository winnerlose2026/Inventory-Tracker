"""Houston OCR only reads a picture that belongs to the reply itself, and
never the same picture twice (review 2026-10-22).

Outlook carries the quoted thread's inline pictures along on a reply, so a
"will do" from Tom could arrive with LAST week's buyer screen -- read as a new
count dated today, it would erase a week of burn and any receipts since."""
import base64
import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import inventory_tracker as it
from integrations import usf_screenshot as S

NEW_HTML = ('<div><p>1055010 – 5 cases a week</p>'
            '<img src="cid:image001.png@01DB0001"></div>'
            '<div id="appendonsend"></div><hr>'
            '<div id="divRplyFwdMsg"><b>From:</b> JD Gross</div>'
            '<blockquote><img src="cid:image002.png@01DB0002"></blockquote>')
THANKS_HTML = ('<p>Will do, thanks!</p>'
               '<div style="border:none;border-top:solid #E1E1E1 1.0pt">'
               '<p><b><span>From:</span></b> Tom Foley</p></div>'
               '<img src="cid:image001.png@01DB0001">')


def test_cids_above_quote():
    assert S.cids_above_quote(NEW_HTML) == {"image001.png@01db0001"}
    assert S.cids_above_quote(THANKS_HTML) == set()
    # No quote at all (the first mail of a thread): every picture is its own.
    assert S.cids_above_quote('<img src="cid:a@b">') == {"a@b"}
    assert S.cids_above_quote("") == set()
    assert S.normalize_cid("<Image001.png@01DB0001>") == "image001.png@01db0001"


def _isolate(tmp: Path, monkeypatch):
    old = it.DATA_DIR
    monkeypatch.setattr(it, "DATA_DIR", tmp)
    for name in dir(it):
        v = getattr(it, name)
        # Earlier tests re-point it.DATA_DIR without restoring it, so match
        # the *_FILE constants by name as well as by directory.
        if isinstance(v, Path) and (v.parent == old or name.endswith("_FILE")):
            monkeypatch.setattr(it, name, tmp / v.name)


def _graph(messages, attachments, html):
    def stub(url, token, extra_headers=None, timeout=30):
        if "/attachments" in url:
            mid = url.split("/messages/")[1].split("/attachments")[0]
            return {"value": attachments.get(mid, [])}
        if "$select=body" in url:
            mid = url.split("/messages/")[1].split("?")[0]
            if "html" in str((extra_headers or {}).get("Prefer", "")):
                return {"body": {"contentType": "html", "content": html.get(mid, "")}}
            return {"body": {"content": "Will do"}}
        return {"value": messages}
    return stub


def test_endpoint_skips_quoted_and_already_ingested_pictures(tmp_path, monkeypatch):
    from blueprints import email as E
    from app import app
    _isolate(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    iso = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    monkeypatch.setenv("MS365_USER", "JD@ms.hhbagels.com")
    monkeypatch.setattr(E, "_graph_token", lambda: "graph-token")
    monkeypatch.setattr(it, "warehouse_freshness", lambda *a, **k: [
        {"warehouse": "Houston, TX", "last_count_at": iso(6)}])
    screen = b"S" * 60_000
    old_screen = b"O" * 60_000
    b64 = lambda b: base64.b64encode(b).decode()
    msgs = [{"id": m, "subject": "RE: Weekly", "sentDateTime": iso(d),
             "receivedDateTime": iso(d), "internetMessageId": f"<{m}>",
             "from": {"emailAddress": {"address": "tom.foley@usfoods.com"}}}
            for m, d in (("fresh", 2), ("thanks", 1))]
    att = {
        "fresh": [
            {"name": "image001.png", "contentType": "image/png", "size": 60_000,
             "isInline": True, "contentId": "<image001.png@01DB0001>",
             "contentBytes": b64(screen)},
            {"name": "image002.png", "contentType": "image/png", "size": 60_000,
             "isInline": True, "contentId": "<image002.png@01DB0002>",
             "contentBytes": b64(old_screen)}],
        "thanks": [
            {"name": "image001.png", "contentType": "image/png", "size": 60_000,
             "isInline": True, "contentId": "<image001.png@01DB0001>",
             "contentBytes": b64(old_screen)}],
    }
    monkeypatch.setattr(E, "_graph_json", _graph(msgs, att,
                                                 {"fresh": NEW_HTML, "thanks": THANKS_HTML}))
    c = app.test_client()
    h = {"X-Inventory-Token": "tok"}
    d = c.get("/api/email/usf-screenshots", headers=h).get_json()
    by = {m["id"]: m for m in d["messages"]}
    assert [i["name"] for i in by["fresh"]["images"]] == ["image001.png"]
    assert by["fresh"]["images"][0]["sha256"] == hashlib.sha256(screen).hexdigest()
    assert [r["name"] for r in by["fresh"]["rejected_images"]] == ["image002.png"]
    assert by["thanks"]["images"] == []                 # quoted copy only
    assert "quoted" in by["thanks"]["rejected_images"][0]["reason"]
    # The cheap pre-check sees no picture in the "thanks" reply either, so the
    # cron won't even install the OCR engine for it.
    # (Graph can't $select contentId there, so for "fresh" it may over-count.)
    d = c.get("/api/email/usf-screenshots?meta_only=1", headers=h).get_json()
    meta = {m["id"]: len(m["images"]) for m in d["messages"]}
    assert meta["thanks"] == 0 and meta["fresh"] >= 1

    # Ingest the fresh screen's events (dry_run=False) -> its hash is recorded
    # and the endpoint refuses that picture from then on.
    ev = S.build_events([], {"7095637": 19.0}, warehouse="Houston, TX",
                        sent_iso=iso(2), message_id="<fresh>", subject="RE: Weekly",
                        sender="tom.foley@usfoods.com",
                        image_sha256=hashlib.sha256(screen).hexdigest())
    r = c.post("/api/email/ingest-events", json={"dry_run": False, "events": ev},
               headers=h)
    assert r.status_code == 200
    d = c.get("/api/email/usf-screenshots", headers=h).get_json()
    fresh = next(m for m in d["messages"] if m["id"] == "fresh")
    assert fresh["images"] == []
    assert any("already ingested" in r["reason"] for r in fresh["rejected_images"])


def test_ocr_script_passes_the_hash_and_logs_rejections(monkeypatch):
    import usf_screenshot_ocr as R
    import test_usf_screenshot as TS
    monkeypatch.setattr(R, "read_screenshot", TS._fake_reader("2026-10-13"))
    msg = TS._msg(body=TS.BODY_1013)
    msg["images"][0]["sha256"] = "ab" * 32
    msg["rejected_images"] = [{"name": "image002.png", "reason": "picture is in "
                               "the quoted thread, not the reply"}]
    res = R.process_message(msg, "Houston, TX")
    assert res["status"] == "ok"
    assert all(e.get("image_sha256") == "ab" * 32 for e in res["events"])
    assert any("image002.png: not read" in n for n in res["notes"])
