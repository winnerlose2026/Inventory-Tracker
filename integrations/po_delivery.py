"""PO delivery tracking with Lineage: scheduled ETA -> changes -> confirmed arrival.

H&H's outbound loads go through Lineage (3PL). Isaiah opens one email thread
per batch -- subject "Quote and Schedule - N Orders" -- listing each PO in a
block (Quantity / Pick-up / Weight / Address / Receiving Hours / PO # /
Invoice #). Lineage (Belinda Morris, Rojean Wilt, Dylan Arnold, the
LTransSolutionsEast inbox) answers on that thread in free text: "the driver
will arrive at 1000AM/18th for pickup and delivery is set for 0800AM/20th",
"Appointment: 10/19/2026 8:00 AM ... PO # 393072B2", "will not deliver on 7/07
... re-requested for 7/10", "Yes, this shows delivered 10/07".

This module is the deterministic half (the free text is read by the Lineage
follow-up scheduled task, which posts structured events here; since 10/15 the
task runs in the cloud with a limited DELIVERY_API_TOKEN and the SERVER writes
and sends the follow-up emails, so no laptop is involved):

  * a per-PO tracking record (data/po_delivery.json) with a small state
    machine -- requested -> scheduled (appointment, may be re-scheduled) ->
    delivered -- and the evidence (sender, send time, verbatim quote) for
    every change;
  * the plausibility rules an event must pass before it can move a date;
  * when a follow-up is due (no appointment yet / appointment passed with no
    delivery confirmation);
  * parsing the PO blocks out of H&H's own request email;
  * checking an event's quote against the message the mailbox really holds,
    and composing the follow-up (fixed recipients and wording).

ARRIVAL IS CONFIRMATION-DRIVEN (JD, 2026-10-15): while a PO is tracked here
and not yet confirmed delivered, the inventory rollover HOLDS it in Pending
POs even after its appointment passes; it lands in on-hand only when Lineage
(or the receiving DC) confirms delivery, dated to the actual delivery time.
An operator who types an explicit arrival on the Pending tab overrides the
hold (arrival_source="operator").
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

CARRIER = "Lineage"

HOLD_STATUSES = ("requested", "scheduled")
EVENTS = ("requested", "scheduled", "rescheduled", "delivered", "released")

# Senders whose word can move a date. H&H's own staff only open requests.
CARRIER_DOMAINS = ("onelineage.com", "lineagelogistics.com")
RECEIVER_DOMAINS = ("usfoods.com", "cheneybrothers.com", "chefswarehouse.com")
HH_DOMAINS = ("hhbagels.com",)

# Follow-up cadence.
SCHEDULE_ASK_AFTER = timedelta(hours=44)     # no appointment ~2 business days in
CONFIRM_ASK_AFTER = timedelta(hours=3)       # appointment passed, no confirmation
REPEAT_ASK_AFTER = timedelta(hours=16)       # at most ~once per run pair

# Plausibility windows for a reported date.
APPT_PAST_LIMIT = timedelta(days=45)
APPT_FUTURE_LIMIT = timedelta(days=90)
DELIVERED_FUTURE_SLACK = timedelta(hours=2)

# Follow-ups the server sends itself (POST /api/pos/delivery/send-followups).
# Recipients are fixed here -- JD chose them 2026-10-15 -- so the caller (the
# cloud scheduled task, holding only the limited DELIVERY_API_TOKEN) can
# trigger a follow-up but never choose who gets it or what it says.
FOLLOWUP_FROM = "jd@hhbagels.com"
FOLLOWUP_TO = ("bmorris@onelineage.com", "LTransSolutionsEast@onelineage.com",
               "rwilt@onelineage.com", "dyarnold@onelineage.com")
FOLLOWUP_CC = ("Isaiah@hhbagels.com", "Gabo@hhbagels.com")
THREAD_QUIET_FOR = timedelta(hours=4)    # someone wrote on the thread just now: wait
SYNC_FRESH_FOR = timedelta(minutes=45)   # follow-ups only off a just-read mailbox
MAX_FOLLOWUP_EMAILS = 10                 # per call
ASK_TEXT = {
    "schedule": ("could you send the pickup date and the delivery appointment "
                 "(date and time)? If it has already delivered, please confirm "
                 "the delivery date and time."),
    "confirm_delivery": ("can you confirm it delivered at its {appt} appointment? "
                         "If it delivered at a different time or hasn't delivered "
                         "yet, please send the actual or updated date and time."),
}
SIGNATURE = ("Thanks,\nJD\n\nJD Gross | Director of Bakery Operations | H&H Bagels\n"
             "office 646-669-8934\n109 West 27th Street, Suite #3B, New York, NY 10001")


def norm_po(s: str) -> str:
    """Same normalisation as core.util._norm_po_key (kept import-free)."""
    t = str(s or "").strip().upper()
    if t.startswith("HHB-") or t.startswith("HHB "):
        t = t[4:]
    return t.strip().strip(".").strip()


def parse_dt(raw: str) -> Optional[datetime]:
    """ISO date/datetime -> aware UTC datetime (naive input read as UTC)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw[-1:] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if not 2020 <= dt.year <= 2100:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None \
        else dt.astimezone(timezone.utc)


def wall_time(raw: str) -> str:
    """The local wall-clock part of an ISO datetime, offset dropped
    ("2026-10-19T08:00:00-05:00" -> "2026-10-19T08:00:00"). This is what the
    Pending tab and the count-absorption date compare should see: the date
    the receiving dock wrote down, not its UTC equivalent."""
    raw = (raw or "").strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?))?", raw)
    if not m:
        return ""
    t = m.group(2) or "00:00:00"
    if len(t) == 5:
        t += ":00"
    return f"{m.group(1)}T{t}"


def _domain(addr: str) -> str:
    return (addr or "").rsplit("@", 1)[-1].lower().strip()


def sender_may_report(addr: str) -> bool:
    d = _domain(addr)
    return any(d == x or d.endswith("." + x)
               for x in CARRIER_DOMAINS + RECEIVER_DOMAINS)


def is_hh(addr: str) -> bool:
    d = _domain(addr)
    return any(d == x or d.endswith("." + x) for x in HH_DOMAINS)


# ---------------------------------------------------------------------------
# H&H's request email
# ---------------------------------------------------------------------------

_PO_LINE = re.compile(r"^\s*PO\s*#?\s*:?\s*([A-Z0-9][A-Z0-9-]{4,15})\b", re.I | re.M)
_FIELD = re.compile(r"^\s*(Quantity|Address|Pick-?up Location|Receiving Hours|"
                    r"Invoice\s*#?)\s*:\s*(.+?)\s*$", re.I | re.M)


def parse_request_blocks(text: str) -> list:
    """PO blocks from an H&H "Quote and Schedule" request body.

    Returns [{po_number, quantity, address, receiving_hours, invoice}] in
    order. Each block is segmented on its own "PO #" line: the Quantity /
    Address / Receiving Hours lines ABOVE it (back to the previous PO line)
    are its own, and the "Invoice #" line just below it. Isaiah doesn't always
    write "Quantity:" ("2 pallets (112 cases) of fresh frozen bagels", 10/02),
    so the PO line is the only reliable separator. A lone "please add PO X"
    follow-up still counts.
    """
    text = text or ""
    pos = list(_PO_LINE.finditer(text))
    out, seen = [], set()
    for i, m in enumerate(pos):
        po = m.group(1).strip()
        key = norm_po(po)
        if key in seen or not re.search(r"\d", key):
            continue
        seen.add(key)
        head = text[(pos[i - 1].end() if i else 0):m.start()]
        tail = text[m.end():(pos[i + 1].start() if i + 1 < len(pos) else len(text))]
        fields = {}
        for k, v in _FIELD.findall(head):
            fields[k.lower().replace("-", "").replace(" ", "").rstrip("#")] = v
        inv = re.search(r"^\s*Invoice\s*#?\s*:\s*(\S+)", tail, re.I | re.M)
        qty = fields.get("quantity", "")
        if not qty:
            q = re.search(r"^\s*(\d+\s+pallets?\b.*?)\s*$", head, re.I | re.M)
            qty = q.group(1) if q else ""
        out.append({
            "po_number": po,
            "quantity": qty,
            "address": fields.get("address", "").rstrip("."),
            "receiving_hours": fields.get("receivinghours", ""),
            "invoice": inv.group(1).strip("# ") if inv else "",
        })
    return out


_SIGNOFF = re.compile(
    r"(The information contained in this message|#exclaimer#|"
    r"This email message and any attachments|CONFIDENTIALITY NOTICE)", re.I)


def clean_body(text: str, limit: int = 1500) -> str:
    """A reply's own text without the legal footer, whitespace squeezed."""
    text = _SIGNOFF.split(text or "", maxsplit=1)[0]
    text = re.sub(r"[ \t ​]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def new_record(po_number: str) -> dict:
    return {"po_number": po_number, "carrier": CARRIER, "status": "requested",
            "requested_at": "", "delivery_appt": "", "pickup_at": "",
            "delivered_at": "", "confirmation_no": "", "appt_evidence_sent": "",
            "thread": {}, "request": {}, "events": [], "chases": [],
            "updated": ""}


def _evidence(ev: dict) -> dict:
    e = ev.get("evidence") or {}
    return {"message_id": str(e.get("message_id") or "")[:300],
            "sender": str(e.get("sender") or "").lower()[:200],
            "sent": str(e.get("sent") or "")[:40],
            "quote": str(e.get("quote") or "").strip()[:600]}


def apply_event(rec: Optional[dict], ev: dict, now: datetime) -> tuple:
    """Apply one event to a PO's record.

    Returns (record, change, error). ``change`` is None for a no-op replay;
    ``error`` (a reason string) means nothing was applied. ``now`` is aware UTC.
    """
    po = str(ev.get("po_number") or "").strip()
    kind = str(ev.get("event") or "").strip().lower()
    if not po:
        return rec, None, "po_number required"
    if kind not in EVENTS:
        return rec, None, f"event must be one of {EVENTS}"
    evid = _evidence(ev)
    rec = dict(rec) if rec else new_record(po)
    rec.setdefault("events", [])
    rec.setdefault("chases", [])
    old = {k: rec.get(k, "") for k in
           ("status", "delivery_appt", "pickup_at", "delivered_at", "confirmation_no")}

    if kind in ("scheduled", "rescheduled", "delivered"):
        if not evid["quote"]:
            return rec, None, "evidence.quote required (the line that says it)"
        if not sender_may_report(evid["sender"]):
            return rec, None, (f"{evid['sender'] or 'unknown sender'} can't report a "
                               "delivery date (Lineage or the receiving DC only)")

    if kind == "requested":
        if not rec.get("requested_at"):
            rec["requested_at"] = evid["sent"] or now.isoformat()
        for k in ("thread", "request"):
            if ev.get(k) and not rec.get(k):
                rec[k] = ev[k]
    elif kind == "released":
        rec["status"] = "released"
    elif kind in ("scheduled", "rescheduled"):
        if rec.get("status") == "delivered" and not ev.get("force"):
            return rec, None, "already confirmed delivered; a schedule can't reopen it"
        appt = parse_dt(ev.get("delivery_appt") or "")
        if appt is None:
            return rec, None, "delivery_appt must be an ISO datetime"
        if not (now - APPT_PAST_LIMIT <= appt <= now + APPT_FUTURE_LIMIT):
            return rec, None, f"delivery_appt {ev.get('delivery_appt')} is implausible"
        # A message older than the one that set the current appointment can't
        # overwrite it (the task may read a thread out of order).
        cur_ev = parse_dt(rec.get("appt_evidence_sent") or "")
        new_ev = parse_dt(evid["sent"])
        if cur_ev and new_ev and new_ev < cur_ev:
            return rec, None, "stale: a newer message already set the appointment"
        rec["status"] = "scheduled"
        rec["delivery_appt"] = str(ev["delivery_appt"]).strip()
        rec["appt_evidence_sent"] = evid["sent"]
        if ev.get("pickup_at"):
            if parse_dt(ev["pickup_at"]) is None:
                return rec, None, "pickup_at must be an ISO date/datetime"
            rec["pickup_at"] = str(ev["pickup_at"]).strip()
        if ev.get("confirmation_no"):
            rec["confirmation_no"] = str(ev["confirmation_no"]).strip()[:40]
    elif kind == "delivered":
        at = parse_dt(ev.get("delivered_at") or "")
        if at is None:
            return rec, None, "delivered_at must be an ISO date/datetime"
        if at > now + DELIVERED_FUTURE_SLACK:
            return rec, None, f"delivered_at {ev.get('delivered_at')} is in the future"
        req = parse_dt(rec.get("requested_at") or "")
        if req and at < req - timedelta(days=1):
            return rec, None, "delivered_at is before the PO was even sent to Lineage"
        rec["status"] = "delivered"
        rec["delivered_at"] = str(ev["delivered_at"]).strip()

    new = {k: rec.get(k, "") for k in old}
    entry = {"event": kind, "at": now.isoformat(), **{k: new[k] for k in new
             if new[k] != old[k]}, "evidence": evid}
    if new == old and kind != "requested":
        return rec, None, None                      # replay: nothing new
    if kind == "requested" and rec["events"]:
        return rec, None, None                      # already registered
    rec["events"] = (rec["events"] + [entry])[-60:]
    rec["updated"] = now.isoformat()
    return rec, {"po_number": rec["po_number"], "event": kind,
                 "before": old, "after": new}, None


def record_chase(rec: dict, ask: str, now: datetime, message_id: str = "") -> dict:
    rec = dict(rec)
    rec["chases"] = (list(rec.get("chases") or []) +
                     [{"at": now.isoformat(), "ask": ask,
                       "message_id": message_id}])[-40:]
    return rec


def _last_chase(rec: dict) -> Optional[datetime]:
    ts = [parse_dt(c.get("at") or "") for c in rec.get("chases") or []]
    ts = [t for t in ts if t]
    return max(ts) if ts else None


def chase_due(rec: dict, now: datetime) -> Optional[str]:
    """'schedule' | 'confirm_delivery' | None for one PO record.

    schedule         -- sent to Lineage ~2 business days ago, still no
                        delivery appointment.
    confirm_delivery -- the appointment passed 3+ hours ago and nobody has
                        confirmed it delivered.
    Never twice within REPEAT_ASK_AFTER.
    """
    st = rec.get("status")
    last = _last_chase(rec)
    if last and now - last < REPEAT_ASK_AFTER:
        return None
    if st == "requested":
        since = max([t for t in (parse_dt(rec.get("requested_at") or ""), last) if t],
                    default=None)
        if since and now - since >= SCHEDULE_ASK_AFTER:
            return "schedule"
    if st == "scheduled":
        appt = parse_dt(rec.get("delivery_appt") or "")
        if appt and now - appt >= CONFIRM_ASK_AFTER:
            return "confirm_delivery"
    return None


# ---------------------------------------------------------------------------
# Evidence check + the follow-up email (server-side, no LLM needed)
# ---------------------------------------------------------------------------

_QUOTE_CHARS = str.maketrans({"‘": "'", "’": "'", "“": '"',
                              "”": '"', "–": "-", "—": "-",
                              " ": " ", "​": " "})


def norm_text(s: str) -> str:
    """Lower-case, straight quotes, single spaces: how a quote is matched."""
    return re.sub(r"\s+", " ", (s or "").translate(_QUOTE_CHARS)).strip().lower()


def quote_in_text(quote: str, text: str) -> bool:
    """Is ``quote`` really in ``text``? Whitespace / quote style don't matter;
    an ellipsis ("..." or "…") may join pieces, each of which must appear in
    order. Guards the events route: a date only moves on a line the mailbox
    actually holds."""
    body = norm_text(text)
    pieces = [norm_text(p) for p in re.split(r"\.\.\.+|…", quote or "")]
    pieces = [p for p in pieces if p]
    if not pieces or sum(len(p) for p in pieces) < 8:
        return False
    at = 0
    for p in pieces:
        i = body.find(p, at)
        if i < 0:
            return False
        at = i + len(p)
    return True


_US_ZONES = (("ET", "America/New_York"), ("CT", "America/Chicago"),
             ("MT", "America/Denver"), ("PT", "America/Los_Angeles"))


def appt_label(raw: str) -> str:
    """'2026-10-19T08:00:00-05:00' -> '10/19 8:00 AM CT' (zone read off the
    offset on that date; '' zone when the offset matches none)."""
    from zoneinfo import ZoneInfo
    raw = (raw or "").strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return wall_time(raw)
    hour = dt.hour % 12 or 12
    label = f"{dt.month}/{dt.day} {hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"
    if dt.tzinfo is None:
        return label
    off = dt.utcoffset()
    for abbr, zone in _US_ZONES:
        if dt.astimezone(ZoneInfo(zone)).utcoffset() == off:
            return f"{label} {abbr}"
    return label


def followup_body(items: list) -> str:
    """The follow-up text. items: [{po_number, distributor, warehouse, ask,
    delivery_appt}] -- one bullet per PO."""
    lines = []
    for it in items:
        where = ", ".join(x for x in (it.get("distributor"), it.get("warehouse")) if x)
        ask = ASK_TEXT[it["ask"]].format(appt=appt_label(it.get("delivery_appt") or ""))
        lines.append(f"- PO {it['po_number']}{f' ({where})' if where else ''}: {ask}")
    return ("Hi team,\n\n" + "\n".join(lines) +
            "\n\nIf any delivery date changes, please let us know on this thread.\n\n"
            + SIGNATURE)


INTERNAL_TEST_PREFIX = "TEST (internal"   # H&H-only test replies: never "activity"


def quiet_pos(messages: list, po_numbers: list, now: datetime) -> dict:
    """POs on a thread that someone wrote about within THREAD_QUIET_FOR ->
    {po_key: reason}. A message that names some of the thread's POs quiets
    only those; one that names none ("This order is all set", "checking,
    will advise") quiets them all. A busy thread about one PO therefore
    doesn't hold back the follow-up on another."""
    quiet = {}
    for m in sorted(messages, key=lambda m: m.get("sent") or ""):
        sent = parse_dt(m.get("sent") or "")
        if not sent or now - sent >= THREAD_QUIET_FOR:
            continue
        text = m.get("text") or ""
        if text.lstrip().startswith(INTERNAL_TEST_PREFIX):
            continue
        up = text.upper()
        named = [p for p in po_numbers
                 if re.search(r"(?<![A-Z0-9])" + re.escape(p) + r"(?![A-Z0-9])", up)]
        mins = int((now - sent).total_seconds() // 60)
        why = (f"{m.get('from') or 'someone'} wrote about it {mins} min ago "
               "-- giving them time")
        for p in (named or po_numbers):
            quiet[p] = why
    return quiet


def reply_subject(subject: str) -> str:
    s = (subject or "").strip()
    return s if re.match(r"^re\s*:", s, re.I) else f"RE: {s}"


def awaiting_confirmation(rec: dict, now: datetime) -> bool:
    appt = parse_dt(rec.get("delivery_appt") or "")
    return rec.get("status") == "scheduled" and bool(appt) and appt <= now


def summary(rec: dict, now: datetime) -> dict:
    """What the Pending POs tab and the follow-up task need to see."""
    last = (rec.get("events") or [{}])[-1]
    lc = _last_chase(rec)
    return {
        "carrier": rec.get("carrier") or CARRIER,
        "status": rec.get("status") or "",
        "requested_at": rec.get("requested_at") or "",
        "delivery_appt": rec.get("delivery_appt") or "",
        "delivery_appt_local": wall_time(rec.get("delivery_appt") or ""),
        "pickup_at": rec.get("pickup_at") or "",
        "delivered_at": rec.get("delivered_at") or "",
        "delivered_local": wall_time(rec.get("delivered_at") or ""),
        "confirmation_no": rec.get("confirmation_no") or "",
        "awaiting_confirmation": awaiting_confirmation(rec, now),
        "chase_due": chase_due(rec, now),
        "last_chased_at": lc.isoformat() if lc else "",
        "chase_count": len(rec.get("chases") or []),
        "last_event": {"event": last.get("event", ""),
                       "quote": (last.get("evidence") or {}).get("quote", ""),
                       "sender": (last.get("evidence") or {}).get("sender", ""),
                       "sent": (last.get("evidence") or {}).get("sent", "")},
        "thread_subject": (rec.get("thread") or {}).get("subject", ""),
        "destination": (rec.get("request") or {}).get("address", ""),
    }
