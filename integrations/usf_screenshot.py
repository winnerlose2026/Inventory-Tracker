"""Read a US Foods buyer-screen SCREENSHOT into on-hand counts.

Houston's area buyer (Tom Foley, NW Houston DC 3350) has no export -- he
answers the weekly "Weekly Bagel Inventory & Usage Report" thread with an
inline picture of his USF buyer screen::

    Dist | Product | Product Description | Vndr Prod # | Pack Size | List | Status | Label | Hand | Order
    3350 | 1055010 | BAGEL, EGG 4.06 Z … | 1184        | 6/10/4.06 | …    | Normal | …     |   27 |    56

and (since 2026-10-13) types the weekly usage into the body as one line per
item, keyed by USF item #::

    1055010 – 5 cases a week

Neither is readable by the mailbox scan (there is no OCR on the web service,
by design: an OCR engine at request time is an OOM risk on the starter plan).
This module is the OCR-free half of the reader: it turns OCR text boxes into a
validated table and both inputs into ingest events. The OCR itself runs off
the web service -- ``scripts/usf_screenshot_ocr.py`` (driven by the 6-hour
Render cron) feeds it boxes from RapidOCR.

Every row must prove itself before it can touch inventory:
  - ``Dist`` equals the warehouse's USF distribution number (3350 = Houston),
    so another DC's screen can never land on Houston;
  - ``Product`` is a known USF item # (``USF_ITEM_NO_TO_MFG``) AND the
    ``Vndr Prod #`` read on the same row is that item's H&H MFG code -- two
    independent reads that must agree, which catches a misread or a row
    shifted onto its neighbour's numbers;
  - ``Hand`` is a plain whole number, and never an H&H MFG code (the classic
    column-mapping fault).
One bad row blocks the whole screenshot: a partial Houston count is worse than
a late one.

``Order`` is read for the log only. It is the open PO (393072B2 on 10/13), which
the PO path already carries; applying it here would double count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

try:
    from .hh_mfg_codes import HH_MFG_CODE_TO_VARIETY
    from .usfoods_inventory_report import (
        CASE_SIZE, REPORT_SENDER_TO_WAREHOUSE, USF_ITEM_NO_TO_MFG,
    )
except ImportError:  # standalone / test use
    from hh_mfg_codes import HH_MFG_CODE_TO_VARIETY  # type: ignore
    from usfoods_inventory_report import (  # type: ignore
        CASE_SIZE, REPORT_SENDER_TO_WAREHOUSE, USF_ITEM_NO_TO_MFG,
    )


DISTRIBUTOR = "US Foods"

# Warehouses whose rep reports as a buyer-screen screenshot -> the USF
# distribution number printed in the screen's Dist column.
SCREENSHOT_DIST_BY_WAREHOUSE: dict[str, str] = {
    "Houston, TX": "3350",   # NW Houston, 13400 Hollister Rd (Tom Foley)
}

# Provenance tag on every event. On-hand rank is the default (40), the same
# as the hand-keyed ingests of these screenshots (manual-houston-screenshot),
# so a re-read of an already hand-entered screenshot is a no-op replay and a
# real spreadsheet (rank 60) would still win on the same date.
PARSER_SOURCE = "usf-screenshot-ocr"

# Below this an inline image is a signature logo, never a buyer screen (USF's
# logos are 15-29 KB; the screens are 320-560 KB).
MIN_SCREEN_IMAGE_BYTES = 40_000

# Rows a real Houston screen carries (13 SKUs). Fewer means the picture was
# cropped or under-read -- don't half-count the warehouse.
MIN_ROWS = 10


def senders_for_warehouse(warehouse: str) -> list[str]:
    """Report reps mapped to ``warehouse`` (lowercased emails)."""
    return sorted(e for e, (_d, wh) in REPORT_SENDER_TO_WAREHOUSE.items()
                  if wh == warehouse)


@dataclass
class Box:
    """One OCR text box, in image pixels."""
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    score: float = 1.0

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def h(self) -> float:
        return max(1.0, self.y1 - self.y0)


def boxes_from_quads(quads, texts, scores=None) -> list:
    """Build Boxes from an OCR engine's 4-point polygons."""
    out = []
    scores = list(scores) if scores is not None else [1.0] * len(texts)
    for q, t, s in zip(quads, texts, scores):
        xs = [float(p[0]) for p in q]
        ys = [float(p[1]) for p in q]
        out.append(Box(str(t or ""), min(xs), max(xs), min(ys), max(ys), float(s)))
    return out


@dataclass
class ScreenRow:
    usf_item_no: str
    dist: str = ""
    vndr: str = ""
    hand: Optional[int] = None
    order: Optional[int] = None
    hand_box: Optional[Box] = None
    order_box: Optional[Box] = None
    y: float = 0.0
    raw: dict = field(default_factory=dict)   # column -> text, for the log


@dataclass
class ScreenTable:
    columns: dict                       # column key -> (x_left, x_right)
    rows: list
    has_order: bool = False


# Header text (squeezed: lowercase, no spaces/punctuation) -> column key.
_HEADER_KEYS = {
    "dist": "dist",
    "product": "product",
    "vndrprod": "vndr",
    "vendorprod": "vndr",
    "productdescription": "desc",
    "packsize": "pack",
    "list": "list",
    "status": "status",
    "label": "label",
    "hand": "hand",
    "onhand": "hand",
    "order": "order",
    "onorder": "order",
}

_SQUEEZE = re.compile(r"[^a-z]")
_LEADING_ITEM = re.compile(r"^\W{0,2}(\d{7})(?!\d)")
_INT = re.compile(r"^\d{1,5}$")
_TRAILING_CODE = re.compile(r"(?<![\d.])(\d{4})\s*$")


def _squeeze(text: str) -> str:
    return _SQUEEZE.sub("", (text or "").lower())


def _clean_int(text: str) -> Optional[int]:
    t = (text or "").strip().replace(",", "")
    # PP-OCR sometimes reads a lone 0 as the letter O.
    t = t.replace("O", "0").replace("o", "0")
    return int(t) if _INT.match(t) else None


def find_header(boxes: list) -> Optional[dict]:
    """Locate the buyer screen's header row. Returns {key: Box} or None.

    Needs at least Product and Hand on one line -- that is what makes an image
    a buyer screen rather than a logo or some other picture.
    """
    found: dict = {}
    for b in boxes:
        key = _HEADER_KEYS.get(_squeeze(b.text))
        if key and key not in found:
            found[key] = b
    if "product" not in found or "hand" not in found:
        return None
    if abs(found["product"].yc - found["hand"].yc) > max(found["hand"].h, 8):
        return None   # not one header line
    return found


def _column_spans(header: dict, image_width: float) -> dict:
    """Column x-ranges: each header's span widened to the midpoint of the gap
    to its neighbours (the first and last run to the image edges)."""
    order = sorted(header.items(), key=lambda kv: kv[1].x0)
    spans = {}
    for i, (key, b) in enumerate(order):
        left = 0.0 if i == 0 else (order[i - 1][1].x1 + b.x0) / 2.0
        right = (image_width if i == len(order) - 1
                 else (b.x1 + order[i + 1][1].x0) / 2.0)
        spans[key] = (left, right)
    return spans


def _overlap(b: Box, span) -> float:
    return max(0.0, min(b.x1, span[1]) - max(b.x0, span[0]))


def _column_of(b: Box, spans: dict) -> Optional[str]:
    best, best_ov = None, 0.0
    for key, span in spans.items():
        ov = _overlap(b, span)
        if ov > best_ov:
            best, best_ov = key, ov
    return best


def parse_buyer_screen(boxes: list, image_width: Optional[float] = None
                       ) -> Optional[ScreenTable]:
    """Assemble OCR boxes into buyer-screen rows; None if not a buyer screen.

    Rows are anchored on the 7-digit USF item # in the Product column (the
    detector sometimes glues it to the description: "1055061  BAGEL, BLBRY…").
    Every other box joins the anchor row it shares a line with, in the column
    its x-extent overlaps most.
    """
    header = find_header(boxes)
    if header is None:
        return None
    width = image_width or max((b.x1 for b in boxes), default=0.0)
    spans = _column_spans(header, width)
    head_y = header["hand"].yc
    row_h = header["hand"].h

    body = [b for b in boxes
            if b.yc > head_y + 0.6 * row_h and b not in header.values()]
    rows: list = []
    for b in body:
        m = _LEADING_ITEM.match(b.text.strip())
        if not m or _overlap(b, spans["product"]) <= 0:
            continue
        if b.x0 > spans["product"][1]:
            continue
        rows.append(ScreenRow(usf_item_no=m.group(1), y=b.yc,
                              raw={"product": b.text}))
    rows.sort(key=lambda r: r.y)
    if not rows:
        return ScreenTable(columns=spans, rows=[], has_order="order" in spans)

    gaps = [b.y - a.y for a, b in zip(rows, rows[1:])]
    pitch = sorted(gaps)[len(gaps) // 2] if gaps else row_h * 1.5
    tol = max(4.0, 0.45 * pitch)

    for b in body:
        near = min(rows, key=lambda r: abs(r.y - b.yc))
        if abs(near.y - b.yc) > tol:
            continue
        col = _column_of(b, spans)
        txt = b.text.strip()
        if col == "dist":
            near.dist = txt
        elif col == "vndr":
            near.vndr = txt
        elif col == "hand":
            if near.hand_box is not None:   # two boxes in one cell -> unreadable
                near.raw["hand_dupe"] = txt
            near.hand_box = b
            near.hand = _clean_int(txt)
        elif col == "order":
            near.order_box = b
            near.order = _clean_int(txt)
        if col:
            near.raw.setdefault(col, txt)
        # The detector sometimes glues the Vndr Prod # onto the end of the
        # description ("I BAGEL, BLBRY WHEAT 4.06 Z UNSL 1171", Tom's 9/29
        # screen). Take a trailing 4-digit token from a box that physically
        # ends inside the Vndr column -- the USF#<->Vndr# check still has to
        # pass, so a wrong pick blocks rather than lands.
        if col != "vndr" and "vndr" in spans:
            vl, vr = spans["vndr"]
            if _overlap(b, spans["vndr"]) >= 20 and b.x1 <= vr + 5:
                tail = _TRAILING_CODE.search(txt)
                if tail:
                    near.raw.setdefault("vndr_tail", tail.group(1))
    for r in rows:
        if not r.vndr and r.raw.get("vndr_tail"):
            r.vndr = r.raw["vndr_tail"]
    return ScreenTable(columns=spans, rows=rows, has_order="order" in spans)


def validate_table(table: ScreenTable, warehouse: str) -> tuple:
    """Check every row (see module docstring). Returns (ok_rows, warnings)."""
    warnings: list = []
    expected_dist = SCREENSHOT_DIST_BY_WAREHOUSE.get(warehouse, "")
    ok_rows: list = []
    seen: set = set()
    for r in table.rows:
        tag = f"row {r.usf_item_no}"
        mfg = USF_ITEM_NO_TO_MFG.get(r.usf_item_no)
        if not mfg:
            warnings.append(f"{tag}: unknown USF item # -- add it to "
                            "usfoods_inventory_report.USF_ITEM_NO_TO_MFG")
            continue
        if expected_dist and re.sub(r"\D", "", r.dist) != expected_dist:
            warnings.append(f"{tag}: Dist {r.dist!r} is not {warehouse}'s "
                            f"{expected_dist}")
            continue
        if re.sub(r"\D", "", r.vndr) != mfg:
            warnings.append(f"{tag}: Vndr Prod # read {r.vndr!r}, expected "
                            f"{mfg} for this item -- row misread")
            continue
        if "hand_dupe" in r.raw:
            warnings.append(f"{tag}: two values in the Hand cell "
                            f"({r.raw.get('hand')!r}, {r.raw['hand_dupe']!r})")
            continue
        if r.hand is None:
            warnings.append(f"{tag}: Hand {r.raw.get('hand', '')!r} is not a "
                            "whole number")
            continue
        if str(r.hand) in HH_MFG_CODE_TO_VARIETY:
            warnings.append(f"{tag}: Hand {r.hand} equals an H&H MFG code -- "
                            "column misread")
            continue
        if r.usf_item_no in seen:
            warnings.append(f"{tag}: appears twice")
            continue
        seen.add(r.usf_item_no)
        ok_rows.append(r)
    if len(table.rows) < MIN_ROWS:
        warnings.append(f"only {len(table.rows)} item rows read (expected "
                        f">= {MIN_ROWS}) -- picture cropped or under-read")
    return ok_rows, warnings


# ---------------------------------------------------------------------------
# Typed usage lines
# ---------------------------------------------------------------------------

# "1055010 – 5 cases a week", "1055010 - 5 cs/wk", "1055010: 5.5 cases per week"
_USAGE_LINE = re.compile(
    r"^\W*(\d{7})\s*[-–—:=]+\s*(\d+(?:\.\d+)?)\s*"
    r"(?:cases?|cs)\b[^\n]*?\b(?:a|per|/)?\s*(?:week|wk)\b",
    re.IGNORECASE)

# Where a reply's own text ends and the quoted thread begins.
_QUOTE_MARKERS = re.compile(
    r"^\s*(?:-{3,}\s*Original Message\s*-{3,}|_{8,}|From:\s|On .+ wrote:)",
    re.IGNORECASE | re.MULTILINE)


def strip_quoted(text: str) -> str:
    """The reply's own text, without the quoted thread below it -- so last
    week's numbers quoted under this week's reply are never re-read."""
    m = _QUOTE_MARKERS.search(text or "")
    return (text or "")[:m.start()] if m else (text or "")


def parse_usage_lines(text: str) -> tuple:
    """Typed per-item weekly usage -> ({usf_item_no: cases_per_week}, unknown).

    ``unknown`` lists USF item #s with a usage line but no MFG mapping.
    """
    usage: dict = {}
    unknown: list = []
    for line in strip_quoted(text).splitlines():
        m = _USAGE_LINE.match(line.strip())
        if not m:
            continue
        item, qty = m.group(1), float(m.group(2))
        if item not in USF_ITEM_NO_TO_MFG:
            unknown.append(item)
            continue
        usage[item] = qty
    return usage, unknown


# ---------------------------------------------------------------------------
# Which pictures belong to THIS reply (not the quoted thread)
# ---------------------------------------------------------------------------

# Where the reply's own HTML ends and the quoted history begins: OWA / new
# Outlook (divRplyFwdMsg, appendonsend), Outlook desktop (the E1E1E1 rule above
# a bold "From:"), Gmail, Apple Mail (blockquote), plain "Original Message".
_HTML_QUOTE_MARKERS = re.compile(
    r"id=[\"']?divRplyFwdMsg"
    r"|id=[\"']?appendonsend"
    r"|class=[\"']?gmail_quote"
    r"|<blockquote"
    r"|border-top:\s*solid\s+#E1E1E1"
    r"|-{3,}\s*Original Message\s*-{3,}"
    r"|<b>\s*(?:<span[^>]*>\s*)?From:",
    re.IGNORECASE)
_CID_REF = re.compile(r"src\s*=\s*[\"']?cid:([^\"'\s>]+)", re.IGNORECASE)


def normalize_cid(cid: str) -> str:
    """Graph contentId ("<image001.png@01DB..>") vs HTML src ("cid:image001...")."""
    return (cid or "").strip().strip("<>").strip().lower()


def cids_above_quote(html: str) -> set:
    """Content-ids of the inline pictures referenced in the reply's OWN part
    of an HTML body -- above the first quoted-history marker.

    Outlook carries the quoted thread's inline pictures along on a reply, so a
    "will do" from Tom used to arrive with LAST week's buyer screen attached;
    read as a fresh count dated today, it would erase a week of burn and any
    receipts since (review 2026-10-22). Only a picture placed in the new part
    of the message is this week's screen.
    """
    text = html or ""
    m = _HTML_QUOTE_MARKERS.search(text)
    own = text[:m.start()] if m else text
    return {normalize_cid(c) for c in _CID_REF.findall(own)}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def _variety(usf_item_no: str) -> str:
    return HH_MFG_CODE_TO_VARIETY[USF_ITEM_NO_TO_MFG[usf_item_no]]


def build_events(rows: list, usage: dict, *, warehouse: str, sent_iso: str,
                 message_id: str, subject: str, sender: str,
                 image_name: str = "", image_sha256: str = "") -> list:
    """Ingest-event dicts (the /api/email/ingest-events shape).

    One ``on_hand`` per screenshot row, carrying ``weekly_usage`` when Tom
    typed it for that item. Typed usage for an item with no screenshot row
    (or a usage-only email) becomes a ``usage_rate`` event, which refreshes
    weekly_usage without touching cases on hand.

    The count is dated to the email's SEND time -- the screen has no date and
    Tom pulls it right before replying (JD's rule for undated reports).
    """
    common = {
        "source_message_id": message_id,
        "source_subject": subject,
        "count_date": sent_iso,
        "source_received_at": sent_iso,
        "source_sender": sender,
        "parser_source": f"{PARSER_SOURCE}:{image_name}" if image_name
                         else PARSER_SOURCE,
    }
    # The screen's content hash rides on each on-hand event; ingest-events
    # records it, and /api/email/usf-screenshots refuses that picture from
    # then on -- the same screen re-sent later is never a new count.
    if image_sha256:
        common["image_sha256"] = image_sha256
    events = []
    counted = set()
    for r in rows:
        item = {"quantity": float(r.hand), "distributor": DISTRIBUTOR,
                "variety": _variety(r.usf_item_no), "warehouse": warehouse,
                "unit": "cs", "case_size": CASE_SIZE,
                "distributor_sku": r.usf_item_no}
        if r.usf_item_no in usage:
            item["weekly_usage"] = usage[r.usf_item_no]
        events.append({"event_type": "on_hand", "item": item, **common})
        counted.add(r.usf_item_no)
    for usf, wk in sorted(usage.items()):
        if usf in counted:
            continue
        events.append({"event_type": "usage_rate", "item": {
            "quantity": 0.0, "distributor": DISTRIBUTOR,
            "variety": _variety(usf), "warehouse": warehouse, "unit": "cs",
            "case_size": CASE_SIZE, "weekly_usage": wk,
            "distributor_sku": usf}, **common})
    return events
