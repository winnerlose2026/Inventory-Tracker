"""Parser for ANSI X12 EDI 810 (Invoice) documents.

Cheney Brothers (Walt Wilcox, 2026-07-06) sends daily EDI 810 invoices in place
of the weekly shipment history we asked for. This extracts, per invoice:
invoice #/date, PO #/date, ship-from DC, ship-to store, money totals, and line
items (Cheney item #, cases, unit price, extended cost, tax, description).

Delimiters are read from the ISA envelope when present (element sep = ISA[3],
component sep = ISA[104], segment terminator = ISA[105]); otherwise sensible
X12 defaults are used, and newline-delimited segments are tolerated.

MONEY CONVENTION — settled 2026-08-04 against Cheney's first real drop
(7 invoices, ``vendor_feeds/cheney/2026-08-04_sample_drop/edi_810/``):

    * TDS (invoice total) and SAC05 (charges) arrive with **implied 2 decimals**
      and no decimal point: ``TDS*167619`` == $1,676.19, ``SAC*C*D270***700``
      == $7.00.
    * TXI (tax) and CTP07 (extended cost) arrive with an **explicit** decimal
      point: ``TXI*LS*6.24`` == $6.24.

``_money()`` therefore honours an explicit decimal point and applies implied
2-decimal scaling only when none is present. That rule is correct per X12 and
reproduces all 7 sample invoices exactly:

    total == sum(line extended) + sum(line tax) + sum(charges)

Reading TDS at face value (the pre-2026-08-04 behaviour) overstated every
invoice total by 100x.

QUANTITY CONVENTION — catch-weight lines (``TP*Y``, e.g. sliced fish, cheese,
deli meat) are invoiced in **pounds**, not cases. Converting them needs BOTH
segments of the line:

    IT1*000030*40.000*LB*2.73*...*VU*5      VU = 5 lb per *unit* (per piece)
    PO4*004*5*LB                            PO4 = 4 units to a case

so the case weight is 4 x 5 = 20 lb and 40 lb is **2 cases**. Cheney's order
guide confirms the same pack ("004 5LB"). Note VU is the per-unit weight, NOT
the case weight -- dividing by VU alone overstates cases by the pack count
(2x, 4x, 8x). PO4's size element is rounded to whole pounds ("008 2LB" for a
1.5 lb unit), so we take the **pack count** from PO4 and the **precise unit
weight** from VU. Where PO4 is absent, pack count falls back to 1 and the line
carries ``case_weight_estimated=True``.

Cross-checked over all 11 catch-weight items in the first drop: 164011 40 lb
-> 2.0 cases, 224012 20 lb -> 2.0, 10127838 12 lb -> 1.0. Partial values are
genuine (238600 at 14.3 lb is one case that weighed under its 16 lb average).
``cases`` is the true case count; ``qty``/``uom`` keep the raw invoiced amount.
Treating LB as cases (the pre-2026-08-04 behaviour) overstated cases 3-10x.

CREDIT MEMOS — BIG07 carries the document type: ``DI`` = invoice,
``CR`` = credit memo (a return). Credits are reported with ``is_credit=True``
and ``sign=-1``; callers MUST apply ``sign`` before adding cases or dollars to
a ledger, or returns will read as deliveries. 2 of the 7 samples are credits.
"""
from __future__ import annotations

import re
from datetime import datetime

# Product-ID qualifiers (IT1 pairs), in the order we prefer them as "item_no".
_ITEM_ID_PREFERENCE = ("VN", "VP", "IN", "BP", "CB", "UK", "UP", "UA")

# Implied decimal places for X12 amounts sent without a decimal point.
_IMPLIED_PLACES = 2

# UOMs that are a weight/volume rather than a shipping unit. On these lines
# IT102 is that measure, and VU (unit weight) converts it back to cases.
_WEIGHT_UOMS = {"LB", "OZ", "KG", "GR", "G", "GA", "GL", "QT", "PT", "LT"}

# BIG07 document-type codes that mean "this reverses a shipment".
_CREDIT_DOC_TYPES = {"CR", "CN", "RT"}


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
    """Parse a plain X12 numeric (quantities, weights). Respects an explicit
    decimal point; returns None when empty/non-numeric. No implied scaling --
    quantities never carry implied decimals in Cheney's feed."""
    s = (v or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        m = re.search(r"-?\d+(\.\d+)?", s)
        return float(m.group(0)) if m else None


def _money(v: str):
    """Parse an X12 monetary amount.

    An explicit decimal point is authoritative. Without one, X12 implied
    decimals apply, which for Cheney is 2 places ("167619" -> 1676.19). See the
    module docstring -- this is verified against the first real drop.
    """
    s = (v or "").strip()
    if not s:
        return None
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if "." in s:
        val = _num(s)
        if val is None:
            return None
    else:
        m = re.search(r"\d+", s)
        if not m:
            return None
        val = int(m.group(0)) / (10 ** _IMPLIED_PLACES)
    return round(-val if neg else val, 2)


def _seg_at(row, i):
    return row[i].strip() if i < len(row) else ""


def _is_weight_uom(uom: str) -> bool:
    return (uom or "").upper() in _WEIGHT_UOMS


def _recompute_cases(line: dict) -> None:
    """Set ``cases`` (and ``case_weight``) on a weight-UOM line.

    case weight = pack count (PO4) x unit weight (VU). Called after IT1 and
    again once PO4 arrives, since PO4 follows IT1 in the segment stream.
    Leaves ``cases`` None when VU is missing -- better to surface an unknown
    than to invent a case count.
    """
    qty, uom, vu = line["qty"], line["uom"], line["unit_weight"]
    if qty is None or not _is_weight_uom(uom):
        return
    if not vu:
        line["cases"] = None
        line["case_weight"] = None
        return
    pack = line["pack_count"] or 1
    case_weight = pack * vu
    line["case_weight"] = round(case_weight, 4)
    line["case_weight_estimated"] = not line["pack_count"]
    line["cases"] = round(qty / case_weight, 3) if case_weight else None


def _parse_it1(row):
    """IT1 baseline item -> line dict. IT101 line, IT102 qty, IT103 UOM,
    IT104 unit price; IT106+ are (qualifier, value) product-ID pairs."""
    line_no = _seg_at(row, 1)
    qty = _num(_seg_at(row, 2))
    uom = _seg_at(row, 3).upper()
    unit_price = _money(_seg_at(row, 4))
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
    unit_weight = _num(ids.get("VU", ""))
    extended = None
    if qty is not None and unit_price is not None:
        extended = round(qty * unit_price, 2)
    line = {
        "line_no": line_no,
        "qty": qty,                      # as invoiced (cases OR pounds)
        "uom": uom,
        # Weight lines get their real case count once PO4 supplies the pack
        # count; plain case lines are already cases.
        "cases": None if _is_weight_uom(uom) else qty,
        "unit_price": unit_price,
        "extended": extended,            # computed qty * unit_price
        "extended_reported": None,       # CTP07, when Cheney states it
        "tax": None,                     # TXI on this line
        "item_no": item_no,
        "description": "",               # PID
        "pack": "",                      # PO4 as "004 5LB"
        "pack_count": None,              # PO401: units per case
        "case_weight": None,             # pack_count x unit_weight
        "case_weight_estimated": False,  # True when PO4 was missing
        "brand": ids.get("BL", ""),
        "mfg_name": ids.get("MF", ""),
        "mfg_code": ids.get("MG", ""),
        "catch_weight": (ids.get("TP", "").upper() == "Y"),
        "unit_weight": unit_weight,      # VU: lb per UNIT (not per case)
        "product_ids": ids,
    }
    _recompute_cases(line)
    return line


def _new_invoice() -> dict:
    return {
        "invoice_number": "", "invoice_date": "", "po_number": "", "po_date": "",
        "doc_type": "", "is_credit": False, "sign": 1,
        "ship_from": "", "ship_from_code": "",
        "ship_to": "", "ship_to_account": "",
        "lines": [],
        "subtotal": None, "tax": None, "charges": None, "total": None,
        "charge_details": [], "unit_count": None, "line_count": None,
    }


def _finalize(inv: dict) -> None:
    """Compute subtotal/tax rollups and check the invoice against its own TDS."""
    lines = inv["lines"]
    inv["subtotal"] = round(sum(l["extended"] or 0.0 for l in lines), 2)
    line_tax = round(sum(l["tax"] or 0.0 for l in lines), 2)
    # A summary TXI (after TDS) restates the line tax; prefer it when present.
    if inv["tax"] is None:
        inv["tax"] = line_tax
    inv["charges"] = round(sum(c["amount"] or 0.0 for c in inv["charge_details"]), 2)
    if inv["total"] is not None:
        expected = round(inv["subtotal"] + (inv["tax"] or 0.0) + inv["charges"], 2)
        inv["variance"] = round(inv["total"] - expected, 2)
        inv["reconciles"] = abs(inv["variance"]) <= 0.02
    else:
        inv["variance"] = None
        inv["reconciles"] = False


def parse_810(text: str) -> list[dict]:
    """Parse one or more X12 810 transactions -> list of invoice dicts.

    Each dict: invoice_number, invoice_date, po_number, po_date, doc_type,
    is_credit, sign, ship_from(+_code), ship_to(+_account), lines[], subtotal,
    tax, charges, total, charge_details[], unit_count, line_count, reconciles,
    variance. See the module docstring for the money/quantity conventions.
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
    in_summary = False   # True once TDS is seen: later TXI is invoice-level
    for row in rows:
        tag = _seg_at(row, 0).upper()
        if tag == "ST" and _seg_at(row, 1) == "810":
            cur = _new_invoice()
            in_summary = False
            invoices.append(cur)
        elif cur is None:
            continue
        elif tag == "BIG":
            cur["invoice_date"] = _iso_date(_seg_at(row, 1))
            cur["invoice_number"] = _seg_at(row, 2)
            cur["po_date"] = _iso_date(_seg_at(row, 3))
            cur["po_number"] = _seg_at(row, 4)
            doc = _seg_at(row, 7).upper()
            cur["doc_type"] = doc
            cur["is_credit"] = doc in _CREDIT_DOC_TYPES
            cur["sign"] = -1 if cur["is_credit"] else 1
        elif tag == "N1":
            qual = _seg_at(row, 1).upper()
            name = _seg_at(row, 2)
            code = _seg_at(row, 4)
            # SF = ship-from DC, SU/WH = supplier/warehouse fallbacks.
            if qual in ("SF", "SU", "WH") and name and not cur["ship_from"]:
                cur["ship_from"] = name
                cur["ship_from_code"] = code
            # ST = ship-to (which H&H store this invoice belongs to).
            elif qual == "ST" and name and not cur["ship_to"]:
                cur["ship_to"] = name
                cur["ship_to_account"] = code
        elif tag == "IT1":
            cur["lines"].append(_parse_it1(row))
        elif tag == "PID" and cur["lines"]:
            # PID*F****<description>
            desc = _seg_at(row, 5)
            if desc and not cur["lines"][-1]["description"]:
                cur["lines"][-1]["description"] = desc
        elif tag == "PO4" and cur["lines"]:
            # PO4*004*5*LB -> pack "004 5LB", 4 units per case. The pack COUNT
            # is what we trust; PO4's size is rounded to whole units, so the
            # precise per-unit weight stays with VU (see module docstring).
            pack, size, puom = _seg_at(row, 1), _seg_at(row, 2), _seg_at(row, 3)
            line = cur["lines"][-1]
            if pack or size:
                line["pack"] = f"{pack} {size}{puom}".strip()
            n = _num(pack)
            if n:
                line["pack_count"] = int(n)
                _recompute_cases(line)
        elif tag == "CTP" and cur["lines"]:
            amt = _money(_seg_at(row, 7))
            if amt is not None:
                cur["lines"][-1]["extended_reported"] = amt
        elif tag == "TXI":
            amt = _money(_seg_at(row, 2))
            if in_summary:
                cur["tax"] = amt
            elif cur["lines"] and amt is not None:
                cur["lines"][-1]["tax"] = amt
        elif tag == "SAC":
            # SAC01 C=charge / A=allowance, SAC02 code, SAC05 amount.
            ind = _seg_at(row, 1).upper()
            amt = _money(_seg_at(row, 5))
            if amt is not None:
                if ind == "A":
                    amt = -amt
                cur["charge_details"].append(
                    {"indicator": ind, "code": _seg_at(row, 2), "amount": amt})
        elif tag == "TDS":
            cur["total"] = _money(_seg_at(row, 1))
            in_summary = True
        elif tag == "ISS":
            cur["unit_count"] = _num(_seg_at(row, 1))
        elif tag == "CTT":
            n = _num(_seg_at(row, 1))
            cur["line_count"] = int(n) if n is not None else None
        elif tag == "SE":
            _finalize(cur)
            cur = None
    for inv in invoices:
        if "reconciles" not in inv:   # document ended without SE
            _finalize(inv)
    return invoices


def summarize(invoices: list[dict]) -> dict:
    """Roll a batch of parsed invoices into one summary, credits subtracted.

    Returns totals plus the integrity signals a daily feed should be watched
    on: which invoices failed to reconcile against their own TDS, and which
    lines could not be converted to cases.
    """
    net = round(sum(i["sign"] * (i["total"] or 0.0) for i in invoices), 2)
    cases = round(sum(i["sign"] * (l["cases"] or 0.0)
                      for i in invoices for l in i["lines"]), 3)
    return {
        "invoices": len(invoices),
        "credits": sum(1 for i in invoices if i["is_credit"]),
        "net_total": net,
        "net_cases": cases,
        "lines": sum(len(i["lines"]) for i in invoices),
        "unreconciled": [i["invoice_number"] for i in invoices if not i["reconciles"]],
        "lines_without_cases": [
            f"{i['invoice_number']}:{l['line_no']} ({l['item_no']} {l['qty']} {l['uom']})"
            for i in invoices for l in i["lines"] if l["cases"] is None
        ],
        "line_count_mismatch": [
            i["invoice_number"] for i in invoices
            if i["line_count"] is not None and i["line_count"] != len(i["lines"])
        ],
        "unit_count_mismatch": [
            i["invoice_number"] for i in invoices
            if i["unit_count"] is not None
            and abs(i["unit_count"] - sum(l["qty"] or 0.0 for l in i["lines"])) > 0.02
        ],
    }


__all__ = ["parse_810", "summarize"]
