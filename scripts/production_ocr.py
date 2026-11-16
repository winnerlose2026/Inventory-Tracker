#!/usr/bin/env python3
"""OCR scanned (image-only) Daily Production sheets into the Inventory Tracker.

When Gabo scans a printed sheet instead of exporting it, the PDF carries no
text and the web service stores it as "could not read" (11/5/2026, Riviera
Beach PO 014511774519). This script, run off the web service by the 6-hour
cron (scripts/cowork_graph_scan.py installs the OCR deps on demand), does:

  1. GET /api/production/unreadable -- image-only sheets of the last 30 days,
     each with its PDF fetched by the web service from the mailbox.
  2. Render each page (pypdfium2) and OCR it (RapidOCR, as for Houston).
  3. Rebuild the sheet text (integrations/production_ocr.sheet_text) and check
     it: the rows must add up to the sheet's "Total Cases", every row needs a
     lot, every SKU a known variety. Anything off -> nothing is stored.
  4. POST /api/production/ingest with the PDF and the OCR text; the server
     parses it with the normal rules and flags the record "read by OCR".

    python scripts/production_ocr.py            # dry run: read + check only
    python scripts/production_ocr.py --commit
    python scripts/production_ocr.py --pdf sheet.pdf --subject "..."   # local test

Creds: APP_URL / INVENTORY_API_TOKEN env, or --app-url / --api-token.
Exit codes: 0 ok or nothing to do | 2 config / network error | 3 blocked.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from integrations.production_ocr import sheet_text  # noqa: E402

# Same engine settings the Houston reader was validated with (bounded RSS on
# the 512 MB cron instance).
_OCR_PARAMS = {
    "Global.use_cls": False,
    "Global.log_level": "warning",
    "Det.limit_type": "max",
    "Det.limit_side_len": 1600,
}
_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR
        _engine = RapidOCR(params=dict(_OCR_PARAMS))
    return _engine


def ocr_pdf(pdf_bytes: bytes) -> list:
    """OCR boxes (x_left, y_center, height, text, score) for every page, the
    pages stacked top to bottom."""
    import numpy as np
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf_bytes)
    boxes, y_off = [], 0
    try:
        for i in range(len(doc)):
            img = doc[i].render(scale=2).to_pil().convert("RGB")
            res = _get_engine()(np.array(img), use_det=True, use_cls=False, use_rec=True)
            if res is not None and getattr(res, "boxes", None) is not None:
                for quad, txt, score in zip(res.boxes, res.txts, res.scores):
                    xs = [p[0] for p in quad]
                    ys = [p[1] for p in quad]
                    boxes.append((float(min(xs)), y_off + sum(ys) / 4.0,
                                  float(max(ys) - min(ys)), str(txt), float(score)))
            y_off += img.height
    finally:
        doc.close()
    return boxes


def check_parse(text: str, subject: str, received_at: str) -> list:
    """Problems with the sheet as the server's parser will read it."""
    from integrations.production_pdf_parser import parse_production_text
    s = parse_production_text(text, subject=subject, received_at=received_at)
    out = []
    if s.error:
        out.append(s.error)
    if s.unmapped_varieties:
        out.append(f"unknown SKU(s): {', '.join(s.unmapped_varieties)}")
    missing = [f"{ln.cs_count} cs {ln.raw_variety}" for ln in s.lines if not ln.lot_number]
    if missing:
        out.append(f"no lot for: {'; '.join(missing)}")
    if not s.po_number:
        out.append("no PO number")
    return out


def _http(url: str, token: str, body: dict | None = None, timeout: int = 180) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"X-Inventory-Token": token,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def read_one(pdf_bytes: bytes, subject: str, received_at: str) -> tuple:
    text, problems = sheet_text(ocr_pdf(pdf_bytes))
    if not problems:
        problems = check_parse(text, subject, received_at)
    return text, problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true", help="store what passes the checks")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--app-url", default=os.environ.get("APP_URL", "").rstrip("/"))
    ap.add_argument("--api-token", default=os.environ.get("INVENTORY_API_TOKEN", ""))
    ap.add_argument("--pdf", help="read a local PDF instead (prints, stores nothing)")
    ap.add_argument("--selftest", action="store_true",
                    help="re-read sheets already stored by OCR and compare (stores nothing)")
    ap.add_argument("--subject", default="")
    a = ap.parse_args(argv)

    if a.pdf:
        text, problems = read_one(Path(a.pdf).read_bytes(), a.subject, "")
        print(text)
        for p in problems:
            print(f"BLOCKED: {p}")
        return 3 if problems else 0

    if not a.app_url or not a.api_token:
        print("ERROR: APP_URL and INVENTORY_API_TOKEN are required", file=sys.stderr)
        return 2
    if a.selftest:
        return selftest(a)
    try:
        data = _http(f"{a.app_url}/api/production/unreadable?days={a.days}", a.api_token)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: production-ocr: listing failed ({type(exc).__name__})", file=sys.stderr)
        return 2
    rc = 0
    for e in data.get("errors") or []:
        print(f"ERROR: production-ocr: {e}", file=sys.stderr)
        rc = 2
    sheets = data.get("sheets") or []
    if not sheets:
        print("production-ocr: no scanned sheets waiting")
        return rc
    for sh in sheets:
        for pdf in sh.get("pdfs") or []:
            label = f"{sh['subject'][:70]!r}"
            try:
                raw = base64.b64decode(pdf["b64"])
                text, problems = read_one(raw, sh["subject"], sh["received_at"])
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR: production-ocr: {label}: OCR failed "
                      f"({type(exc).__name__}: {exc})", file=sys.stderr)
                rc = 3
                continue
            if problems:
                print(f"BLOCKED: production-ocr: {label}: " + "; ".join(problems),
                      file=sys.stderr)
                rc = 3
                continue
            n = text.count(" CS ")
            if not a.commit:
                print(f"production-ocr: {label}: read OK ({n} rows) -- dry run, not stored")
                continue
            try:
                out = _http(f"{a.app_url}/api/production/ingest", a.api_token, {
                    "pdf_b64": pdf["b64"], "subject": sh["subject"],
                    "sender": sh.get("sender") or "", "message_id": sh["key"],
                    "received_at": sh["received_at"], "ocr_text": text})
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR: production-ocr: {label}: ingest failed "
                      f"({type(exc).__name__})", file=sys.stderr)
                rc = 2
                continue
            rec = out.get("record") or {}
            print(f"production-ocr: {label}: {out.get('status')} -- PO "
                  f"{rec.get('po_number')} {rec.get('warehouse')}, "
                  f"{rec.get('total_cases')} cs, {len(rec.get('lines') or [])} rows")
            # "duplicate": the other runner (10-minute cron vs the 6-hour
            # scan) stored it a moment earlier -- fine.
            if out.get("status") not in ("ingested", "replaced", "refreshed", "duplicate"):
                rc = 3
    return rc


def selftest(a) -> int:
    """Prove the OCR on this machine: re-read every sheet already stored by
    OCR (last ``--days``) and compare rows + lots with what is stored.
    Stores nothing. 0 = all match, 3 = a mismatch, 2 = could not run."""
    from integrations.production_pdf_parser import parse_production_text
    try:
        data = _http(f"{a.app_url}/api/production/unreadable?selftest=1&days={a.days}",
                     a.api_token)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: production-ocr selftest: listing failed ({type(exc).__name__})",
              file=sys.stderr)
        return 2
    sheets = [sh for sh in data.get("sheets") or [] if sh.get("pdfs")]
    if not sheets:
        print("production-ocr SELFTEST: no OCR-read sheets to re-read")
        return 2
    rc = 0
    for sh in sheets:
        text, problems = read_one(base64.b64decode(sh["pdfs"][0]["b64"]),
                                  sh["subject"], sh["received_at"])
        s = parse_production_text(text, subject=sh["subject"], received_at=sh["received_at"])
        got = sorted((l.variety, l.cs_count, l.lot_number) for l in s.lines)
        want = sorted((l["variety"], l["cs_count"], l["lot_number"])
                      for l in sh.get("stored_lines") or [])
        ok = not problems and got == want and s.po_number == sh.get("stored_po")
        print(f"production-ocr SELFTEST {sh['subject'][:60]!r}: "
              f"{'MATCH' if ok else 'MISMATCH'} ({len(got)} rows, PO {s.po_number})"
              + ("" if ok else f" problems={problems} got={got} want={want}"))
        rc = rc if ok else 3
    return rc


if __name__ == "__main__":
    sys.exit(main())
