"""Parser for ANSI X12 EDI 810 (Invoice) documents.

Cheney Brothers (Walt Wilcox, 2026-07-06) will send daily EDI 810 invoices in
place of the weekly shipment history we asked for. This extracts the fields we
need per invoice: invoice #/date, PO #/date, ship-from DC, and line items
(Cheney item #, cases, unit price, extended cost).

Delimiters are read from the ISA envelope when present (element sep = ISA[3],
component sep = ISA[104], segment terminator = ISA[105]); otherwise sensible
X12 defaults are used, and newline-delimited segments are tolerated.

IMPORTANT — validate against Cheney's FIRST real 810 before trusting the money
fields: X12 amounts can carry *implied* decimals (e.g. "106000" == 1060.00).
This parser respects an explicit decimal point and otherwise takes the number
at face value; if Cheney sends implied-decimal amounts, adjust _num()/scaling
here. The item#, quantities, PO# and dates are unaffected by that convention.
"""
from __future__ import annotations

import re
from datetime import datetime

# Product-ID qualifiers (IT1 pairs), in the order we prefer them as "item_no".
_ITEM_ID_PREFERENCE = ("VN", "VP", "IN", "BP", "CB", "UK", "UP", "UA")


def _delims(text: str):
    if text[:3] == "ISA" and len(text) >= 106:
        return text[3], text[104], text[105]
    return "*", ">", "~"


def _iso_date(v: str) -> str:
    """X12 date (CCYYMMDD or YYMMDD) -> ISO 'YYYY-MM-DD', else ''."""
    s = re.sub(r"\D", "", (v or ""))
    try:
        if len(s) == 8:
            return datetime.strptime(s, "%Y%m%d").strftime("%Y-%m-%d")
        if len(s) == 6:
            return datetime.strptime(s, "%y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""
    return ""


def _num(v: str):
    """Parse an X12 numeric. Respects an explicit decimal point; returns None
    when empty/non-numeric. Does NOT apply implied decimals (see module note)."""
    s = (v or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        m = re.search(r"-?\d+(\.\d+)?", s)
        return float(m.group(0)) if m else None


def _seg_at(row, i):
    return row[i].strip() if i < len(row) else ""


def _parse_it1(row):
    """IT1 baseline item -> {line_no, cases, uom, unit_price, extended,
    item_no, product_ids}. IT101 line, IT102 qty, IT103 UOM, IT104 unit price;
    IT106+ are (qualifier, value) product-ID pairs."""
    line_no = _seg_at(row, 1)
    qty = _num(_seg_at(row, 2))
    uom = _seg_at(row, 3)
    unit_price = _num(_seg_at(row, 4))
    ids: dict[str, str] = {}
    i = 6
    while i + 1 < len(row):
        qual = _seg_at(row, i).upper()
        val = _seg_at(row, i + 1)
        if qual and val:
            ids.setdefault(qual, val)
        i += 2
    item_no = ""
    for q in _ITEM_ID_PREFERENCE:
        if ids.get(q):
            item_no = ids[q]
            break
    if not item_no and ids:
        item_no = next(iter(ids.values()))
    extended = None
    if qty is not None and unit_price is not None:
        extended = round(qty * unit_price, 2)
    return {
        "line_no": line_no,
        "cases": qty,
        "uom": uom,
        "unit_price": unit_price,
        "extended": extended,
        "item_no": item_no,
        "product_ids": ids,
    }


def parse_810(text: str) -> list[dict]:
    """Parse one or more X12 810 transactions -> list of invoice dicts:
    {invoice_number, invoice_date, po_number, po_date, ship_from, lines[], total}.
    """
    text = (text or "").strip()
    if not text:
        return []
    elem, _comp, seg = _delims(text)
    segs = text.split(seg) if seg in text else text.splitlines()
    rows = [s.replace("\r", "").replace("\n", "").split(elem)
            for s in segs if s.strip()]

    invoices: list[dict] = []
    cur = None
    for row in rows:
        tag = _seg_at(row, 0).upper()
        if tag == "ST" and _seg_at(row, 1) == "810":
            cur = {"invoice_number": "", "invoice_date": "", "po_number": "",
                   "po_date": "", "ship_from": "", "lines": [], "total": None}
            invoices.append(cur)
        elif cur is None:
            continue
        elif tag == "BIG":
            cur["invoice_date"] = _iso_date(_seg_at(row, 1))
            cur["invoice_number"] = _seg_at(row, 2)
            cur["po_date"] = _iso_date(_seg_at(row, 3))
            cur["po_number"] = _seg_at(row, 4)
        elif tag == "N1":
            qual = _seg_at(row, 1).upper()
            name = _seg_at(row, 2)
            # SF = ship-from, SU = supplier/seller, WH = warehouse
            if qual in ("SF", "SU", "WH") and name and not cur["ship_from"]:
                cur["ship_from"] = name
        elif tag == "IT1":
            cur["lines"].append(_parse_it1(row))
        elif tag == "TDS":
            cur["total"] = _num(_seg_at(row, 1))
        elif tag == "SE":
            cur = None
    return invoices
