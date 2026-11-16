"""Rebuild a scanned Daily Production sheet's text from OCR boxes.

Gabo sometimes scans the printed sheet instead of exporting it (11/5/2026,
"DAILY PRODUCTION RIVIERA BEACH PO.014511774519": an image-only PDF). pypdf
gets no text from it, so the sheet sat as "could not read" and PO
014511774519 had no lots. scripts/production_ocr.py (run by the 6-hour cron,
never by the web service) OCRs the page; this module turns the OCR boxes into
the same plain text a text PDF yields, so integrations.production_pdf_parser
reads it with the very same rules -- and refuses anything that does not add
up. Pure: no OCR engine imported here.

A box is (x_left, y_center, height, text, score).
"""
from __future__ import annotations

import re
import statistics

_CS_RE = re.compile(r"^\s*(\d{1,3}(?:,\d{3})*|\d+)\s*CS\b\s*(.*)$", re.I)
_LOT_RE = re.compile(r"^\d{10,12}$")
_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
_TOTAL_RE = re.compile(r"(\d{1,3}(?:,\d{3})*|\d+)\s*Total\s*Cases", re.I)
_HEADER_RE = re.compile(r"([A-Z][A-Z .\-]*?)\s*\.\s*PO\s*\.\s*([A-Z0-9]{5,16})", re.I)
#: Each CS count must be read at least this confidently.
MIN_COUNT_SCORE = 0.80


def rows_from_boxes(boxes: list) -> list:
    """Group boxes into visual rows (top to bottom), each left to right."""
    if not boxes:
        return []
    tol = 0.6 * statistics.median(b[2] for b in boxes)
    rows: list = []
    for b in sorted(boxes, key=lambda b: b[1]):
        if rows and abs(rows[-1][-1][1] - b[1]) <= tol:
            rows[-1].append(b)
        else:
            rows.append([b])
    return [sorted(r, key=lambda b: b[0]) for r in rows]


def sheet_text(boxes: list) -> tuple:
    """(text for parse_production_text, problems). Problems non-empty means
    the read is not trusted: nothing should be stored from it."""
    header = date = ""
    total = None
    lines, lots, problems = [], [], []
    for row in rows_from_boxes(boxes):
        texts = [b[3].strip() for b in row if b[3].strip()]
        joined = " ".join(texts)
        if not header and (m := _HEADER_RE.search(joined)):
            header = f"{m.group(1).strip().upper()}.PO.{m.group(2).upper()}"
            continue
        if total is None and (m := _TOTAL_RE.search(joined)):
            total = int(m.group(1).replace(",", ""))
            continue
        if not date and (m := _DATE_RE.search(joined)):
            date = m.group(1)
        row_lots = [t for t in texts if _LOT_RE.match(t.replace(" ", ""))]
        lots.extend(t.replace(" ", "") for t in row_lots)
        rest = [b for b in row if b[3].strip() not in row_lots]
        if rest and (m := _CS_RE.match(rest[0][3])):
            sku = " ".join([m.group(2)] + [b[3].strip() for b in rest[1:]]).strip()
            if rest[0][4] < MIN_COUNT_SCORE:
                problems.append(f"case count {rest[0][3]!r} read with low "
                                f"confidence ({rest[0][4]:.2f})")
            if not sku:
                problems.append(f"no SKU beside {rest[0][3]!r}")
                continue
            lines.append((int(m.group(1).replace(",", "")), sku.upper()))
    if not header:
        problems.append("no '<WAREHOUSE>.PO.<number>' header found")
    if not lines:
        problems.append("no '<n> CS <SKU>' rows found")
    if total is None:
        problems.append("no 'Total Cases' figure found")
    elif lines and sum(n for n, _ in lines) != total:
        problems.append(f"rows add up to {sum(n for n, _ in lines)} cs but the "
                        f"sheet says {total} Total Cases")
    if lines and len(lots) < len(lines):
        problems.append(f"{len(lines)} rows but only {len(lots)} lot number(s)")
    text = "Production Date\n" + (f"{date}\n" if date else "")
    text += "CS Count SKU\n" + "".join(f"{n} CS {sku}\n" for n, sku in lines)
    text += f"{total if total is not None else ''} Total Cases{header}\nLot#\n"
    text += "".join(f"{lot}\n" for lot in lots)
    return text, problems
