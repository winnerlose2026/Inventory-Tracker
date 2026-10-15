"""Houston buyer-screen screenshots -> on-hand counts (integrations/usf_screenshot,
scripts/usf_screenshot_ocr, the /api/email/usf-screenshots route and the cron
step in scripts/cowork_graph_scan).

Tom Foley (NW Houston, USF Dist 3350) reports as an inline picture of his
buyer screen plus usage typed in the body. Fixtures are his real 9/22, 9/29
and 10/13 screens: the PNGs, and the OCR boxes RapidOCR 3.10 produced for them
(so the table logic is tested without the OCR engine installed). The engine
test at the bottom runs only where rapidocr is installed.
"""
import base64
import copy
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "scripts"))

import usf_screenshot_ocr as R  # noqa: E402
from integrations import usf_screenshot as S  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "usf_screenshot"

# Hand column as printed, by USF item #.
TRUTH = {
    "2026-09-22": {"1055010": 35, "1055061": 30, "1055064": 60, "1055066": 8,
                   "1055074": 113, "1137644": 33, "1198923": 40, "1528283": 52,
                   "2954526": 51, "6950804": 78, "7095637": 130, "7309056": 145,
                   "7928199": 44},
    "2026-09-29": {"1055010": 35, "1055061": 26, "1055064": 53, "1055066": 8,
                   "1055074": 108, "1137644": 30, "1198923": 34, "1528283": 46,
                   "2954526": 46, "6950804": 58, "7095637": 110, "7309056": 115,
                   "7928199": 42},
    "2026-10-13": {"1055010": 27, "1055061": 13, "1055064": 39, "1055066": 0,
                   "1055074": 96, "1137644": 21, "1198923": 26, "1528283": 38,
                   "2954526": 32, "6950804": 42, "7095637": 72, "7309056": 72,
                   "7928199": 32},
}
ORDER_1013 = {"1055010": 56, "1055061": 56, "1055064": 56, "1055066": 0,
              "1055074": 112, "1137644": 56, "1198923": 56, "1528283": 56,
              "2954526": 56, "6950804": 112, "7095637": 224, "7309056": 224,
              "7928199": 56}

BODY_1013 = (
    "Here is the on hand on order.\r\n\r\n[cid:image001.png@01DD5]\r\n\r\n"
    "1055010 – 5 cases a week\r\n1055061 – 7 cases a week\r\n"
    "1055064 – 8 cases a week\r\n1055066 – 2 cases a week\r\n"
    "1055074 – 8 cases a week\r\n1137644 – 5 cases a week\r\n"
    "1198923 – 5 cases a week\r\n1528283 – 6 cases a week\r\n"
    "2954526 – 9 cases a week\r\n6950804 – 12 cases a week\r\n"
    "7095637 – 19 cases a week\r\n7309056 – 22 cases a week\r\n"
    "7928199 – 5 cases a week\r\n\r\nTom Foley | Area Buyer\r\n\r\n"
    "-----Original Message-----\r\nFrom: JD Gross <JD@hhbagels.com>\r\n"
    "1055010 – 99 cases a week\r\n"
)


def _boxes(day):
    data = json.loads((FIX / f"houston_{day}.boxes.json").read_text())
    return ([S.Box(t, x0, x1, y0, y1, sc) for t, x0, x1, y0, y1, sc in data["boxes"]],
            data["image_width"])


def _table(day):
    boxes, width = _boxes(day)
    return S.parse_buyer_screen(boxes, image_width=width)


# ---------------------------------------------------------------------------
# Table assembly + validation
# ---------------------------------------------------------------------------

def test_all_three_real_screens_read_every_hand_value():
    for day, truth in TRUTH.items():
        rows, warnings = S.validate_table(_table(day), "Houston, TX")
        assert warnings == [], (day, warnings)
        assert {r.usf_item_no: r.hand for r in rows} == truth, day


def test_order_column_is_read_but_only_for_the_log():
    table = _table("2026-10-13")
    assert table.has_order
    assert {r.usf_item_no: r.order for r in table.rows} == ORDER_1013
    rows, _ = S.validate_table(table, "Houston, TX")
    events = S.build_events(rows, {}, warehouse="Houston, TX",
                            sent_iso="2026-10-13T14:26:49Z", message_id="<m>",
                            subject="s", sender="tom.foley@usfoods.com")
    # The 1,120 cs on order is PO 393072B2 -- never written as on-hand.
    assert sum(e["item"]["quantity"] for e in events) == 510
    assert all(e["event_type"] == "on_hand" for e in events)


def test_vndr_code_glued_to_the_description_is_recovered():
    """9/29: the detector merged 'BAGEL, BLBRY WHEAT 4.06 Z UNSL 1171' into
    one box. The trailing code is taken -- and still has to match."""
    table = _table("2026-09-29")
    row = next(r for r in table.rows if r.usf_item_no == "1055061")
    assert row.vndr == "1171"
    assert row.raw.get("vndr_tail") == "1171"


def test_a_screen_from_another_dc_is_refused():
    boxes, width = _boxes("2026-10-13")
    for b in boxes:
        if b.text == "3350":
            b.text = "3351"
            break
    rows, warnings = S.validate_table(S.parse_buyer_screen(boxes, width),
                                      "Houston, TX")
    assert any("Dist '3351'" in w for w in warnings), warnings
    assert len(rows) == 12


def test_a_row_whose_vndr_code_disagrees_with_its_item_is_refused():
    boxes, width = _boxes("2026-10-13")
    for b in boxes:
        if b.text == "1184":   # Egg's vendor code read as Plain's
            b.text = "1150"
    rows, warnings = S.validate_table(S.parse_buyer_screen(boxes, width),
                                      "Houston, TX")
    assert any("row 1055010" in w and "1150" in w for w in warnings), warnings


def test_hand_equal_to_an_mfg_code_is_refused():
    boxes, width = _boxes("2026-10-13")
    table = S.parse_buyer_screen(boxes, width)
    table.rows[0].hand = 1184
    _, warnings = S.validate_table(table, "Houston, TX")
    assert any("equals an H&H MFG code" in w for w in warnings), warnings


def test_a_cropped_screen_is_refused():
    boxes, width = _boxes("2026-10-13")
    table = S.parse_buyer_screen(boxes, width)
    table.rows = table.rows[:6]
    _, warnings = S.validate_table(table, "Houston, TX")
    assert any("only 6 item rows" in w for w in warnings), warnings


def test_a_picture_without_the_header_is_not_a_buyer_screen():
    boxes, width = _boxes("2026-10-13")
    boxes = [b for b in boxes if b.text not in ("Hand",)]
    assert S.parse_buyer_screen(boxes, width) is None
    assert S.parse_buyer_screen([S.Box("US FOODS", 0, 90, 0, 20)], 100) is None


# ---------------------------------------------------------------------------
# Typed usage
# ---------------------------------------------------------------------------

def test_usage_lines_parse_and_quoted_thread_is_ignored():
    usage, unknown = S.parse_usage_lines(BODY_1013)
    assert unknown == []
    assert usage["1055010"] == 5.0          # not the 99 quoted below
    assert usage["7309056"] == 22.0
    assert sum(usage.values()) == 113.0 and len(usage) == 13


def test_usage_line_variants_and_unknown_items():
    text = ("1055010 - 5.5 cs/wk\n7095637: 19 cases per week\n"
            "1234567 – 3 cases a week\nPO updated to 10/19.\n")
    usage, unknown = S.parse_usage_lines(text)
    assert usage == {"1055010": 5.5, "7095637": 19.0}
    assert unknown == ["1234567"]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def test_events_carry_usage_and_the_send_time_as_count_date():
    rows, _ = S.validate_table(_table("2026-10-13"), "Houston, TX")
    usage, _ = S.parse_usage_lines(BODY_1013)
    events = S.build_events(rows, usage, warehouse="Houston, TX",
                            sent_iso="2026-10-13T14:26:49Z", message_id="<abc>",
                            subject="RE: Weekly", sender="tom.foley@usfoods.com",
                            image_name="image001.png")
    plain = next(e for e in events if e["item"]["variety"] == "Plain")
    assert plain["event_type"] == "on_hand"
    assert plain["item"]["quantity"] == 72 and plain["item"]["weekly_usage"] == 19
    assert plain["item"]["warehouse"] == "Houston, TX"
    assert plain["item"]["distributor_sku"] == "7095637"
    assert plain["count_date"] == plain["source_received_at"] == "2026-10-13T14:26:49Z"
    assert plain["parser_source"] == "usf-screenshot-ocr:image001.png"
    assert len(events) == 13


def test_usage_without_a_screen_row_is_a_usage_rate_event():
    events = S.build_events([], {"7095637": 19.0}, warehouse="Houston, TX",
                            sent_iso="2026-10-20T15:00:00Z", message_id="<m>",
                            subject="s", sender="tom.foley@usfoods.com")
    assert [e["event_type"] for e in events] == ["usage_rate"]
    assert events[0]["item"]["weekly_usage"] == 19.0


def test_screenshot_ocr_lands_at_the_hand_keyed_rank():
    """Same rank as the manual-houston-screenshot ingests, so re-reading a
    screenshot that was already hand-entered is a replay, and a spreadsheet
    (rank 60) would still win on the same date."""
    from sync_inventory import _onhand_source_rank, _DEFAULT_ONHAND_RANK
    assert _onhand_source_rank("usf-screenshot-ocr:image001.png") == \
        _onhand_source_rank("manual-houston-screenshot") == _DEFAULT_ONHAND_RANK


# ---------------------------------------------------------------------------
# process_message: which replies count, which block
# ---------------------------------------------------------------------------

def _fake_reader(day):
    def read(img_bytes, warehouse):
        table = _table(day)
        rows, warnings = S.validate_table(table, warehouse)
        return {"table": table, "rows": rows, "warnings": warnings, "notes": []}
    return read


def _msg(subject="RE: Weekly Bagel Inventory & Usage Report — H&H Bagels",
         body="", images=True):
    img = [{"name": "image001.png",
            "b64": base64.b64encode(b"x" * 50_000).decode()}] if images else []
    return {"sender": "tom.foley@usfoods.com", "subject": subject,
            "sent": "2026-10-13T14:26:49Z", "message_id": "<m>",
            "body_text": body, "images": img}


def test_process_message_statuses(monkeypatch):
    monkeypatch.setattr(R, "read_screenshot", _fake_reader("2026-10-13"))
    ok = R.process_message(_msg(body=BODY_1013), "Houston, TX")
    assert ok["status"] == "ok" and len(ok["events"]) == 13

    usage_only = R.process_message(_msg(body=BODY_1013, images=False), "Houston, TX")
    assert usage_only["status"] == "usage-only"
    assert {e["event_type"] for e in usage_only["events"]} == {"usage_rate"}

    po_chatter = R.process_message(
        _msg(subject="RE: H&H Bagels - PO # 393072B2",
             body="PO updated to 10/19.", images=False), "Houston, TX")
    assert po_chatter["status"] == "skipped" and po_chatter["events"] == []

    def not_a_screen(img_bytes, warehouse):
        return {"table": None, "rows": [], "warnings": [],
                "notes": ["not a buyer screen"]}
    monkeypatch.setattr(R, "read_screenshot", not_a_screen)
    odd = R.process_message(_msg(), "Houston, TX")
    assert odd["status"] == "blocked" and odd["events"] == []


def test_one_bad_row_blocks_the_whole_screen(monkeypatch):
    def bad(img_bytes, warehouse):
        table = _table("2026-10-13")
        table.rows[3].vndr = "9999"
        rows, warnings = S.validate_table(table, warehouse)
        return {"table": table, "rows": rows, "warnings": warnings, "notes": []}
    monkeypatch.setattr(R, "read_screenshot", bad)
    res = R.process_message(_msg(body=BODY_1013), "Houston, TX")
    assert res["status"] == "blocked" and res["events"] == []


def test_small_images_are_never_ocrd(monkeypatch):
    called = []
    monkeypatch.setattr(R, "read_screenshot", lambda *a: called.append(1))
    msg = _msg(images=False)
    msg["images"] = [{"name": "logo.png",
                      "b64": base64.b64encode(b"x" * 20_000).decode()}]
    R.process_message(msg, "Houston, TX")
    assert called == []


# ---------------------------------------------------------------------------
# /api/email/usf-screenshots
# ---------------------------------------------------------------------------

def _graph_stub(messages_by_user, attachments, bodies):
    def stub(url, token, extra_headers=None, timeout=30):
        if "/attachments" in url:
            mid = url.split("/messages/")[1].split("/attachments")[0]
            return {"value": attachments.get(mid, [])}
        if "$select=body" in url:
            mid = url.split("/messages/")[1].split("?")[0]
            return {"body": {"content": bodies.get(mid, "")}}
        user = url.split("/users/")[1].split("/")[0].replace("%40", "@")
        return {"value": messages_by_user.get(user, [])}
    return stub


def _gmsg(mid, sent, sender="tom.foley@usfoods.com", imid=None):
    return {"id": mid, "subject": "RE: Weekly", "sentDateTime": sent,
            "receivedDateTime": sent, "internetMessageId": imid or f"<{mid}>",
            "from": {"emailAddress": {"address": sender}}}


def test_endpoint_returns_pending_replies_with_their_pictures(monkeypatch):
    from datetime import datetime, timedelta, timezone
    import inventory_tracker
    from blueprints import email as E
    from app import app

    now = datetime.now(timezone.utc)
    iso = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    monkeypatch.setenv("MS365_USER", "JD@ms.hhbagels.com,info@ms.hhbagels.com")
    monkeypatch.setattr(E, "_graph_token", lambda: "graph-token")
    monkeypatch.setattr(inventory_tracker, "warehouse_freshness", lambda *a, **k: [
        {"warehouse": "Houston, TX", "last_count_at": iso(3)}])
    png = base64.b64encode(b"p" * 10).decode()
    monkeypatch.setattr(E, "_graph_json", _graph_stub(
        {"JD@ms.hhbagels.com": [
            _gmsg("new", iso(1)),                      # after the count
            _gmsg("counted", iso(5)),                  # before it
            _gmsg("old", iso(20)),                     # outside lookback
            _gmsg("other", iso(1), sender="x@usfoods.com"),
         ],
         "info@ms.hhbagels.com": [_gmsg("new-copy", iso(1), imid="<new>")]},
        {"new": [{"name": "image001.png", "contentType": "image/png",
                  "size": 561422, "contentBytes": png},
                 {"name": "logo.png", "contentType": "image/png",
                  "size": 20310, "contentBytes": png},
                 {"name": "po.pdf", "contentType": "application/pdf",
                  "size": 90000, "contentBytes": png}]},
        {"new": "1055010 – 5 cases a week"}))

    c = app.test_client()
    assert c.get("/api/email/usf-screenshots").status_code == 401
    d = c.get("/api/email/usf-screenshots",
              headers={"X-Inventory-Token": "tok"}).get_json()
    assert d["ok"], d
    assert [m["id"] for m in d["messages"]] == ["new"]   # deduped across mailboxes
    m = d["messages"][0]
    assert m["pending"] and m["body_text"].startswith("1055010")
    assert [i["name"] for i in m["images"]] == ["image001.png"]
    assert m["images"][0]["b64"] == png

    d = c.get("/api/email/usf-screenshots?pending_only=0&meta_only=1",
              headers={"X-Inventory-Token": "tok"}).get_json()
    assert [m["id"] for m in d["messages"]] == ["counted", "new"]
    assert all("b64" not in i for m in d["messages"] for i in m["images"])

    bad = c.get("/api/email/usf-screenshots?warehouse=Zebulon,%20NC",
                headers={"X-Inventory-Token": "tok"})
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# The cron step
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Proc:
    def __init__(self, rc, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _cron(monkeypatch, pending, reader_rc=0, deps_ok=True):
    import subprocess
    import cowork_graph_scan as G
    calls = []
    monkeypatch.setattr(G.urllib.request, "urlopen", lambda req, timeout=0: _Resp(
        {"ok": True, "last_count_at": "2026-10-13T14:26:49Z", "messages": pending}))

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["-c", "import rapidocr, onnxruntime"]:
            return _Proc(0 if deps_ok else 1)
        return _Proc(reader_rc, "usf-screenshots: reader output")
    monkeypatch.setattr(subprocess, "run", run)
    rc = G._run_screenshot_ocr("https://app", "tok", dry_run=False)
    return rc, calls


def test_cron_does_nothing_when_no_reply_is_pending(monkeypatch):
    rc, calls = _cron(monkeypatch, [])
    assert rc == 0 and calls == []


def test_cron_reads_and_commits_a_pending_screenshot(monkeypatch):
    rc, calls = _cron(monkeypatch, [{"images": [{"name": "image001.png"}]}])
    assert rc == 0
    reader = calls[-1]
    assert reader[1].endswith("usf_screenshot_ocr.py") and "--commit" in reader


def test_cron_fails_the_run_when_the_reader_blocks(monkeypatch):
    rc, _ = _cron(monkeypatch, [{"images": [{"name": "image001.png"}]}],
                  reader_rc=3)
    assert rc == 1


def test_cron_skips_the_ocr_install_for_text_only_replies(monkeypatch):
    rc, calls = _cron(monkeypatch, [{"images": []}], deps_ok=False)
    assert rc == 0
    assert all(c[1:3] != ["-c", "import rapidocr, onnxruntime"] for c in calls)


# ---------------------------------------------------------------------------
# The real engine (only where rapidocr is installed: the cron, the desktop VM)
# ---------------------------------------------------------------------------

def test_real_ocr_reads_every_screen(monkeypatch):
    import pytest
    pytest.importorskip("rapidocr")
    for day, truth in TRUTH.items():
        got = R.read_screenshot((FIX / f"houston_{day}.png").read_bytes(),
                                "Houston, TX")
        assert got["warnings"] == [], (day, got["warnings"])
        assert {r.usf_item_no: r.hand for r in got["rows"]} == truth, day
