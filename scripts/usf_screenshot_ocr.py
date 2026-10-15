#!/usr/bin/env python3
"""OCR a US Foods buyer-screen screenshot (Houston) into the Inventory Tracker.

Tom Foley answers the weekly report thread with a PICTURE of his USF buyer
screen (Dist / Product / Vndr Prod # / ... / Hand / Order) and types the weekly
usage in the body ("1055010 – 5 cases a week"). The web service can't read a
picture (no OCR engine there, by design), so this script does it off-service:

  1. GET /api/email/usf-screenshots -- the web service pulls the rep's recent
     mail through its own Graph credentials and returns each message that is
     newer than the warehouse's last count ("pending"), with its inline images
     and plain-text body.
  2. OCR each image with RapidOCR (PP-OCRv6), assemble the table, and validate
     every row (integrations/usf_screenshot.py): Dist = 3350, USF item # and
     Vndr Prod # must agree, Hand a whole number. Each Hand value is then read
     a SECOND time from its own cropped cell; the two reads must match.
  3. POST the on_hand events (+ typed weekly usage) to /api/email/ingest-events.

One bad row blocks that message's screenshot entirely (nothing half-counted);
the script then exits 3 so the Render cron run fails and Render emails JD.

Runs from the 6-hour Render cron (scripts/cowork_graph_scan.py installs the
OCR deps on demand and calls this) or by hand:

    python scripts/usf_screenshot_ocr.py                 # dry run vs live data
    python scripts/usf_screenshot_ocr.py --commit
    python scripts/usf_screenshot_ocr.py --image shot.png --sent 2026-10-13T14:26:49Z \
        [--body-file reply.txt] [--commit]

Deps: requirements-ocr-usf.txt (rapidocr 3.10 + onnxruntime).
Creds: APP_URL / INVENTORY_API_TOKEN env, or --app-url / --api-token.

Exit codes: 0 ok or nothing pending | 2 config / network error | 3 blocked.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))     # repo root (integrations)

from integrations.usf_screenshot import (  # noqa: E402
    MIN_SCREEN_IMAGE_BYTES, PARSER_SOURCE, boxes_from_quads, build_events,
    parse_buyer_screen, parse_usage_lines, validate_table, _clean_int,
    _variety,
)

DEFAULT_WAREHOUSE = "Houston, TX"
EXIT_OK, EXIT_ERROR, EXIT_BLOCKED = 0, 2, 3

# Validated 39/39 on Tom's 9/22, 9/29 and 10/13 screens. limit_type=max keeps
# the ~1100 px screen at native size (peak RSS ~330 MB vs ~420 MB when the
# default upscales it), which matters on a 512 MB Render instance.
_OCR_PARAMS = {
    "Global.use_cls": False,
    "Global.log_level": "warning",
    "Det.limit_type": "max",
    "Det.limit_side_len": 1600,
}
_REPORT_SUBJECT = re.compile(r"inventory|usage|stock|on ?hand", re.I)

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR  # heavy; imported lazily
        _engine = RapidOCR(params=dict(_OCR_PARAMS))
    return _engine


def _to_rgb_array(img_bytes: bytes):
    import numpy as np
    from PIL import Image
    return np.array(Image.open(io.BytesIO(img_bytes)).convert("RGB"))


def ocr_boxes(arr) -> list:
    res = _get_engine()(arr, use_det=True, use_cls=False, use_rec=True)
    if res is None or getattr(res, "boxes", None) is None:
        return []
    return boxes_from_quads(res.boxes, res.txts, res.scores)


def _reread_cell(arr, box, pad: int = 4) -> str:
    """Recognise one cell again, alone: crop the box (padded) out of the
    original screenshot, enlarge it 3x and run recognition only."""
    import numpy as np
    from PIL import Image
    h, w = arr.shape[:2]
    x0, x1 = max(0, int(box.x0) - pad), min(w, int(box.x1) + pad)
    y0, y1 = max(0, int(box.y0) - pad), min(h, int(box.y1) + pad)
    crop = Image.fromarray(arr[y0:y1, x0:x1])
    crop = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
    res = _get_engine()(np.array(crop), use_det=False, use_cls=False, use_rec=True)
    txts = getattr(res, "txts", None) or ()
    return (txts[0] if txts else "").strip()


def read_screenshot(img_bytes: bytes, warehouse: str) -> dict:
    """OCR one image. Returns {table, rows (validated), warnings, notes}."""
    arr = _to_rgb_array(img_bytes)
    boxes = ocr_boxes(arr)
    table = parse_buyer_screen(boxes, image_width=float(arr.shape[1]))
    if table is None:
        return {"table": None, "rows": [], "warnings": [],
                "notes": ["not a buyer screen (no Product/Hand header)"]}
    rows, warnings = validate_table(table, warehouse)
    for r in rows:
        again = _reread_cell(arr, r.hand_box)
        if _clean_int(again) != r.hand:
            warnings.append(f"row {r.usf_item_no}: Hand read {r.hand} then "
                            f"{again!r} on a second, cell-only read")
    return {"table": table, "rows": rows, "warnings": warnings, "notes": []}


def process_message(msg: dict, warehouse: str) -> dict:
    """Turn one rep message into events. status: ok | usage-only | skipped |
    blocked."""
    subject = msg.get("subject", "")
    usage, unknown = parse_usage_lines(msg.get("body_text", ""))
    warnings, notes, rows, image_name = [], [], [], ""
    if unknown:
        warnings.append(f"usage typed for unmapped USF item #s {sorted(unknown)} "
                        "-- add them to USF_ITEM_NO_TO_MFG")
    screens = []
    for img in msg.get("images") or []:
        raw = base64.b64decode(img.get("b64") or "")
        if len(raw) < MIN_SCREEN_IMAGE_BYTES:
            continue
        got = read_screenshot(raw, warehouse)
        notes += [f"{img.get('name')}: {n}" for n in got["notes"]]
        if got["table"] is not None:
            screens.append((img.get("name", ""), got))
    if len(screens) > 1:
        warnings.append(f"{len(screens)} buyer screens in one email -- "
                        "can't tell which is current")
    elif screens:
        image_name, got = screens[0]
        warnings += got["warnings"]
        rows = got["rows"]
    if not screens and not usage:
        # A big picture on the report thread that isn't a buyer screen is a
        # format change worth hearing about; anywhere else it's just mail.
        status = "blocked" if (msg.get("images") and
                               _REPORT_SUBJECT.search(subject)) else "skipped"
        if status == "blocked":
            warnings.append("report-thread picture is not a readable buyer "
                            "screen and no usage lines were typed")
        return {"status": status, "events": [], "warnings": warnings,
                "notes": notes, "rows": [], "usage": usage}
    if warnings:
        return {"status": "blocked", "events": [], "warnings": warnings,
                "notes": notes, "rows": rows, "usage": usage}
    events = build_events(
        rows, usage, warehouse=warehouse, sent_iso=msg.get("sent", ""),
        message_id=msg.get("message_id") or msg.get("id", ""),
        subject=subject, sender=msg.get("sender", ""), image_name=image_name)
    return {"status": "ok" if rows else "usage-only", "events": events,
            "warnings": [], "notes": notes, "rows": rows, "usage": usage}


def _print_result(msg: dict, res: dict) -> None:
    print(f"\n=== {msg.get('sender')}  sent {msg.get('sent')}  "
          f"[{res['status'].upper()}]\n    {msg.get('subject', '')[:90]}")
    for r in sorted(res["rows"], key=lambda r: _variety(r.usf_item_no)):
        wk = res["usage"].get(r.usf_item_no)
        print(f"    {_variety(r.usf_item_no):24} usf={r.usf_item_no} "
              f"hand={r.hand:>5}  order={'' if r.order is None else r.order:>5}"
              f"  usage={'' if wk is None else wk:>5}")
    if not res["rows"] and res["usage"]:
        for usf, wk in sorted(res["usage"].items()):
            print(f"    {_variety(usf):24} usf={usf} usage={wk}")
    for n in res["notes"]:
        print("    note:", n)
    for w in res["warnings"]:
        print("    WARN:", w)


def _http(method: str, url: str, token: str, payload=None, timeout=180):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-Inventory-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_messages(base: str, token: str, warehouse: str, lookback: int,
                   include_all: bool, meta_only: bool = False) -> dict:
    q = urllib.parse.urlencode({
        "warehouse": warehouse, "lookback_days": lookback,
        "pending_only": "0" if include_all else "1",
        "meta_only": "1" if meta_only else "0"})
    return _http("GET", f"{base.rstrip('/')}/api/email/usf-screenshots?{q}", token)


def _manual_message(a) -> dict:
    with open(a.image, "rb") as fh:
        raw = fh.read()
    body = ""
    if a.body_file:
        with open(a.body_file, encoding="utf-8", errors="replace") as fh:
            body = fh.read()
    return {"sender": a.sender, "subject": a.subject, "sent": a.sent,
            "message_id": a.message_id or f"manual:{os.path.basename(a.image)}",
            "body_text": body,
            "images": [{"name": os.path.basename(a.image),
                        "b64": base64.b64encode(raw).decode()}]}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--commit", action="store_true",
                   help="Apply. Default: dry run (the server previews the changes).")
    p.add_argument("--warehouse", default=DEFAULT_WAREHOUSE)
    p.add_argument("--lookback-days", type=int, default=10)
    p.add_argument("--all", action="store_true",
                   help="Also re-read messages already counted (testing).")
    p.add_argument("--image", help="Read this screenshot file instead of mail.")
    p.add_argument("--body-file", help="With --image: the reply's text body.")
    p.add_argument("--sent", default="", help="With --image: send time (ISO, Z).")
    p.add_argument("--subject", default="Weekly Bagel Inventory & Usage Report")
    p.add_argument("--sender", default="tom.foley@usfoods.com")
    p.add_argument("--message-id", default="")
    p.add_argument("--no-post", action="store_true",
                   help="Print the reading only; don't call ingest-events.")
    p.add_argument("--app-url", default=os.environ.get(
        "APP_URL", "https://bagel-inventory.onrender.com"))
    p.add_argument("--api-token", default=os.environ.get("INVENTORY_API_TOKEN", ""))
    a = p.parse_args(argv)

    needs_server = not (a.image and a.no_post)
    if needs_server and not a.api_token:
        print("usf-screenshots: no API token (INVENTORY_API_TOKEN / --api-token)",
              file=sys.stderr)
        return EXIT_ERROR

    if a.image:
        if not a.sent:
            print("usf-screenshots: --image needs --sent (the email's send time)",
                  file=sys.stderr)
            return EXIT_ERROR
        messages = [_manual_message(a)]
    else:
        try:
            data = fetch_messages(a.app_url, a.api_token, a.warehouse,
                                  a.lookback_days, a.all)
        except Exception as exc:  # noqa: BLE001 -- network / HTTP
            print(f"usf-screenshots: endpoint failed: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if not data.get("ok"):
            print(f"usf-screenshots: endpoint error: {data.get('error')}",
                  file=sys.stderr)
            return EXIT_ERROR
        for e in data.get("errors") or []:
            print("usf-screenshots: endpoint note:", e)
        messages = data.get("messages") or []
        print(f"usf-screenshots: {a.warehouse} last counted "
              f"{data.get('last_count_at')}; {len(messages)} "
              f"{'message(s)' if a.all else 'pending message(s)'}")
    if not messages:
        return EXIT_OK

    to_post, blocked = [], 0
    for msg in sorted(messages, key=lambda m: m.get("sent", "")):
        try:
            res = process_message(msg, a.warehouse)
        except Exception as exc:  # noqa: BLE001 -- OCR engine failure
            res = {"status": "blocked", "events": [], "rows": [], "usage": {},
                   "notes": [], "warnings": [f"OCR failed: {type(exc).__name__}: {exc}"]}
        _print_result(msg, res)
        if res["status"] == "blocked":
            blocked += 1
        to_post += res["events"]

    if to_post and not a.no_post:
        payload = {"dry_run": not a.commit, "source": PARSER_SOURCE,
                   "messages_seen": len(messages),
                   "messages_parsed": len(messages) - blocked,
                   "errors": [], "events": to_post}
        try:
            resp = _http("POST", f"{a.app_url.rstrip('/')}/api/email/ingest-events",
                         a.api_token, payload)
        except Exception as exc:  # noqa: BLE001
            print(f"usf-screenshots: ingest-events failed: {exc}", file=sys.stderr)
            return EXIT_ERROR
        rep = (resp.get("reports") or [{}])[0]
        print(f"\nusf-screenshots: {'COMMITTED' if a.commit else 'DRY RUN'} "
              f"{len(to_post)} events: updated={rep.get('updated')} "
              f"unchanged={rep.get('unchanged')} status={rep.get('status')}")
        for c in rep.get("changes") or []:
            print(f"    {c.get('name', ''):48} {c.get('old_quantity')} -> "
                  f"{c.get('new_quantity')}  wu {c.get('old_weekly_usage')} -> "
                  f"{c.get('new_weekly_usage')}")
        for k in ("rejected", "stale_skipped", "unmatched"):
            for line in rep.get(k) or []:
                print(f"    {k}: {line}")
        if rep.get("status") not in (None, "ok"):
            return EXIT_ERROR
    if blocked:
        print(f"\nusf-screenshots: BLOCKED {blocked} message(s) -- nothing from "
              "them was applied; see WARN lines.")
        return EXIT_BLOCKED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
