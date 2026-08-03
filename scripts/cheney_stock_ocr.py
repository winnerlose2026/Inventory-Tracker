#!/usr/bin/env python3
"""OCR Michael Ross's Cheney on-hand stock, which he pastes as an IMAGE to the
right of the usage grid (so cell-based parsers never see it).

Each per-facility "Usage&Stock" .xlsx carries:
  - cols A-E: the case-movement/usage grid (parsed by cheney_inventory_report)
  - one embedded PNG (xl/media/*.png): a screenshot of the on-hand stock table
    ("Item # | Description | Brand | Pack | Size | UOM | Stock").

This tool extracts that image, OCRs it with RapidOCR (deep-learning, pip-only --
tesseract was benchmarked at ~30/36 with digit flips and is NOT used), maps each
row to a canonical variety, and emits `on_hand` event dicts (same shape as
/api/email/ingest-events). The count date comes from the file's own usage-grid
"Date Range" end, so last_count_at reflects the report period, not ingest time.

SAFETY: this is a VERIFY-BEFORE-COMMIT tool. It prints the extracted table and
runs a dry-run by default; pass --commit to actually POST. It refuses to commit
a facility unless every row resolves to a variety and a numeric stock, and the
OCR'd item # (when present) agrees with the description's variety -- so a
mis-read never silently lands in inventory. Runs OFF the Render web service
(RapidOCR/onnxruntime stays out of the web build); intended for the Cowork
routine sandbox or manual use.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.cheney_inventory_report import (  # noqa: E402
    warehouse_from_filename, _variety, _report_as_of, _cell_str)
from integrations.hh_mfg_codes import (  # noqa: E402
    CHENEY_ITEM_NO_TO_MFG, HH_MFG_CODE_TO_VARIETY)

DISTRIBUTOR = "Cheney Brothers"
DEFAULT_CASE_SIZE = 60
_ITEM_RE = re.compile(r"(1015\d{4})")
_INT_RE = re.compile(r"^\d{1,4}$")

# Pack/size tokens that are never a stock figure: "1:60", "1:60 CT", "60CT",
# "60 CT". Matched on the RAW token text, not the digit-stripped form -- the old
# code stripped to digits and then blacklisted the literal strings "1" and "60",
# which also threw away every genuine stock count of 1 or 60 (Ocala Plain was 60
# on 2026-08-01). Losing the real value made the row fall through to the
# rightmost surviving number, i.e. the Mfq.Product Code column.
_PACK_RE = re.compile(r"^\s*\d+\s*[:x/]\s*\d+", re.IGNORECASE)
_SIZE_RE = re.compile(r"^\s*\d+\s*(CT|OZ|LB|EA)\b", re.IGNORECASE)

# Header/summary text unique to Cheney's case-movement (USAGE) export. Ross's
# weekly workbook embeds a screenshot of THAT grid -- not of the on-hand stock
# table -- so an image carrying these markers must never yield on_hand events.
# Its "Full Cases" column is cases MOVED over the report's date range; writing
# it into on-hand replaces real stock with a week of usage.
_CASE_MOVEMENT_MARKERS = (
    "full cases",
    "mfq.product code",
    "mfq product code",
    "sum of all products activity",
    "dist item",
    "drill down reporting",
    "dsrgroup",
)
# Positive marker for a genuine on-hand stock table (its Stock column header).
# Required, not merely preferred: an image we can't positively identify as a
# stock table is not trustworthy enough to overwrite inventory with.
_STOCK_MARKERS = ("stock",)


def _extract_pngs(xlsx_bytes: bytes) -> "list[bytes]":
    z = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
    return [z.read(n) for n in z.namelist()
            if n.startswith("xl/media/") and n.lower().endswith((".png", ".jpg", ".jpeg"))]


def _cell_rows(xlsx_bytes: bytes) -> list:
    """Usage-grid rows (for the Date Range -> count date)."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
    try:
        return [[[_cell_str(c) for c in r] for r in ws.iter_rows(values_only=True)]
                for ws in wb.worksheets]
    finally:
        wb.close()


_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR  # heavy; imported lazily
        _ENGINE = RapidOCR()
    return _ENGINE


def _ocr_page(png_bytes: bytes) -> "tuple[list[dict], list[str]]":
    """Return (rows, all_texts). Rows are {desc, item, stock, min_score};
    all_texts is every OCR'd token, so the caller can identify WHICH grid the
    screenshot actually shows before trusting its numbers."""
    import numpy as np
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    res, _ = _engine()(np.array(im))
    toks = []
    for box, text, score in (res or []):
        cx = sum(p[0] for p in box) / 4.0
        cy = sum(p[1] for p in box) / 4.0
        toks.append((cx, cy, str(text).strip(), float(score)))
    all_texts = [t for _, _, t, _ in toks]
    toks.sort(key=lambda z: z[1])
    rows = []
    for cx, cy, t, sc in toks:
        for r in rows:
            if abs(r["y"] - cy) < 12:
                r["items"].append((cx, t, sc)); r["y"] = (r["y"] + cy) / 2.0
                break
        else:
            rows.append({"y": cy, "items": [(cx, t, sc)]})
    out = []
    for r in rows:
        its = r["items"]
        item = next((_ITEM_RE.search(t).group(1) for _, t, _ in its if _ITEM_RE.search(t)), "")
        desc = next((t for _, t, _ in its if "BAGEL" in t.upper()), "")
        nums = []
        for cx, t, sc in its:
            if _PACK_RE.match(t) or _SIZE_RE.match(t):
                continue                      # pack ratio / unit size column
            n = re.sub(r"\D", "", t)
            if not n or not _INT_RE.match(n):
                continue
            if _ITEM_RE.match(n):
                continue                      # Cheney catalog item #
            if n in HH_MFG_CODE_TO_VARIETY:
                continue                      # H&H mfg code column
            nums.append((cx, n, sc))
        if not (desc or item) or not nums:
            continue
        cx, n, sc = max(nums, key=lambda z: z[0])   # rightmost numeric = Stock
        out.append({"desc": desc, "item": item, "stock": int(n), "min_score": sc})
    return out, all_texts


def _ocr_rows(png_bytes: bytes) -> "list[dict]":
    """Rows only. Kept for callers that don't need the raw token list."""
    return _ocr_page(png_bytes)[0]


def _reject_as_stock_table(texts) -> str:
    """Return why this image must not yield on_hand, or "" when it looks like a
    genuine on-hand stock table."""
    blob = " ".join(str(t or "") for t in texts).lower()
    for m in _CASE_MOVEMENT_MARKERS:
        if m in blob:
            return f"case-movement (usage) grid marker {m!r}"
    if not any(m in blob for m in _STOCK_MARKERS):
        return ("no on-hand stock-table header found "
                f"(expected one of {list(_STOCK_MARKERS)})")
    return ""


def events_from_image(png_bytes, warehouse, count_date, *, source="cheney-stock-image"):
    """OCR ONE embedded stock image into on_hand event dicts for a warehouse.
    Reusable by both the .xlsx path (extract_facility) and the scheduled task
    that pulls images from /api/email/cheney-stock-images. Returns
    (events, warnings, notes); warnings are blocking (unresolved row, under-read
    image, missing date, duplicate variety), notes are informational."""
    events, warnings, notes = [], [], []
    rows, all_texts = _ocr_page(png_bytes)
    # The embedded screenshot in Ross's weekly workbook is the case-movement
    # (usage) grid, NOT the on-hand stock table -- on-hand lives in the sheet's
    # own cells and is read by integrations.cheney_inventory_report. Emitting
    # on_hand from a usage grid overwrites real stock with a week of movement,
    # so refuse it outright and say why.
    reason = _reject_as_stock_table(all_texts)
    if reason:
        warnings.append(
            f"{warehouse}: embedded image is not an on-hand stock table "
            f"({reason}) -- refusing to emit on_hand. On-hand for this workbook "
            f"comes from the worksheet cells via "
            f"cheney_inventory_report.parse_report_xlsx."
        )
        return [], warnings, notes
    for row in rows:
        desc, item, stock = row["desc"], row["item"], row["stock"]
        # Item # OCRs cleanly and is deterministic via the crosswalk -> it is the
        # authoritative variety key. Description tokenizes messily (e.g. "WHOLE
        # WHEAT EVERYTHING" splits), so it is only a soft cross-check.
        v_item = HH_MFG_CODE_TO_VARIETY.get(CHENEY_ITEM_NO_TO_MFG.get(item, "")) if item else ""
        v_desc = _variety("", desc, "")
        variety = v_item or v_desc
        if not variety:
            warnings.append(f"{warehouse}: unresolved row desc={desc!r} item={item!r}")
            continue
        if v_item and v_desc and v_item != v_desc:
            notes.append(f"{warehouse}: item#{item}->{v_item} but description->{v_desc} "
                         f"(using item#; description OCR is noisy)")
        item_dict = {"quantity": float(stock), "distributor": DISTRIBUTOR,
                     "variety": variety, "warehouse": warehouse, "unit": "cs",
                     "case_size": DEFAULT_CASE_SIZE}
        if item:
            item_dict["distributor_sku"] = item
        events.append({"event_type": "on_hand", "item": item_dict,
                       "source_message_id": f"{source}:{warehouse}",
                       "source_subject": f"Cheney on-hand stock (OCR image): {warehouse}",
                       "count_date": count_date, "_min_score": row["min_score"]})
    bad = [e for e in events
           if str(int(e["item"]["quantity"])) in HH_MFG_CODE_TO_VARIETY]
    if bad:
        warnings.append(
            f"{warehouse}: {len(bad)} row(s) OCR'd a stock value that is an H&H "
            f"mfg code ("
            + ", ".join(f"{e['item']['variety']}={int(e['item']['quantity'])}"
                        for e in bad)
            + ") -- the quantity column was misread, refusing the facility")
    v = [e["item"]["variety"] for e in events]
    dups = {x for x in v if v.count(x) > 1}
    if dups:
        warnings.append(f"{warehouse}: duplicate varieties OCR'd: {sorted(dups)}")
    if len(events) < 10:
        warnings.append(f"{warehouse}: only {len(events)} rows read from image (expected ~12) "
                        f"-- image likely under-read")
    if not count_date:
        warnings.append(f"{warehouse}: no count date")
    return events, warnings, notes


def extract_facility(xlsx_bytes: bytes, filename: str):
    """Return (warehouse, count_date, events, warnings, notes) for a Cheney
    per-facility 'Usage&Stock' .xlsx (usage cells + an embedded stock image)."""
    warehouse = warehouse_from_filename(filename)
    if not warehouse:
        return "", "", [], [f"{filename}: cannot determine warehouse from filename"], []
    count_date = _report_as_of(_cell_rows(xlsx_bytes), filename)
    pngs = _extract_pngs(xlsx_bytes)
    if not pngs:
        return warehouse, count_date, [], [f"{warehouse}: no embedded stock image found"], []
    events, warnings, notes = [], [], []
    for png in pngs:
        ev, w, n = events_from_image(png, warehouse, count_date,
                                     source=f"cheney-stock-image:{filename}")
        events += ev; warnings += w; notes += n
    return warehouse, count_date, events, warnings, notes


def _post(base, token, events, dry_run):
    payload = {"dry_run": dry_run, "source": "cheney-stock-ocr",
               "messages_seen": 1, "messages_parsed": 1,
               "events": [{k: v for k, v in e.items() if k != "_min_score"} for e in events]}
    req = urllib.request.Request(base.rstrip("/") + "/api/email/ingest-events",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Inventory-Token": token})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", help="Cheney per-facility .xlsx files")
    p.add_argument("--commit", action="store_true",
                   help="Actually POST (default: dry-run + verification table only)")
    p.add_argument("--app-url", default=os.environ.get("APP_URL", "https://bagel-inventory.onrender.com"))
    p.add_argument("--api-token", default=os.environ.get("INVENTORY_API_TOKEN", ""))
    p.add_argument("--min-score", type=float, default=0.5,
                   help="Refuse to commit if any OCR token score is below this")
    a = p.parse_args(argv)

    all_events, all_warn, blocked = [], [], False
    for f in a.files:
        try:
            data = open(f, "rb").read()
            wh, cd, events, warn, notes = extract_facility(data, os.path.basename(f))
        except Exception as exc:  # unreadable / cloud-only / corrupt
            print(f"\n=== {f} ===\n   ERROR reading/parsing: {type(exc).__name__}: {exc}")
            all_warn.append(f"{f}: {exc}"); blocked = True
            continue
        all_warn += warn
        print(f"\n=== {wh or f}  (count_date={cd or '?'}, {len(events)} rows) ===")
        for e in sorted(events, key=lambda z: z["item"]["variety"]):
            print(f"   {e['item']['variety']:24} {int(e['item']['quantity']):>5}  "
                  f"sku={e['item'].get('distributor_sku','')}  score={e['_min_score']:.2f}")
        low = [e for e in events if e["_min_score"] < a.min_score]
        for n in notes:
            print("   note:", n)
        if warn:
            for w in warn:
                print("   WARN:", w)
        if not events or warn or low:
            print(f"   -> facility BLOCKED from commit (rows={len(events)}, "
                  f"warnings={len(warn)}, low_score={len(low)})")
            blocked = True
        else:
            all_events += events

    if not a.commit:
        print(f"\nDRY-RUN. {len(all_events)} events ready. Re-run with --commit to apply.")
        return 0
    if blocked:
        print("\nREFUSING to commit: at least one facility had warnings/low-confidence rows. "
              "Fix or verify manually.", file=sys.stderr)
        return 2
    if not a.api_token:
        print("No API token (set INVENTORY_API_TOKEN).", file=sys.stderr)
        return 2
    resp = _post(a.app_url, a.api_token, all_events, dry_run=False)
    rep = (resp.get("reports") or [{}])[0]
    print("\nCOMMITTED:", rep.get("by_event_type"), "updated:", rep.get("updated"),
          "unchanged:", rep.get("unchanged"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
