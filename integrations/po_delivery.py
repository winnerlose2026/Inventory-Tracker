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
EVENTS = ("requested", "scheduled", "rescheduled", "picked_up", "delivered", "released")

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


# ---------------------------------------------------------------------------
# Does a delivery confirmation give a TIME, or only a date?
# ---------------------------------------------------------------------------
# Half of Lineage's confirmations carry no time ("Yes, this shows delivered
# 10/07.", "This shows it delivered 10/21.", "This shows delivered."), and the
# task filled one in (12:00 local, or the appointment). The rollover then
# treated that made-up time as exact: a 09:00 CT count on the delivery day
# was "stamped before the confirmed delivery" and the whole PO was added on
# top of a count that already held it (review 2026-10-22). The record now
# says whether its time is real (delivered_time_known); only a real time may
# use the exact-time rules.

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
           "nov", "dec")
_DATE_TOKEN = re.compile(
    r"\b(?P<iy>\d{4})-(?P<im>\d{2})-(?P<id>\d{2})\b"                    # 2026-10-19
    r"|\b(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/\d{2,4})?\b"                  # 10/19[/2026]
    r"|\b(?P<mn>" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(?P<md>\d{1,2})"
    r"(?:st|nd|rd|th)?\b(?:,?\s*\d{4}\b)?"                              # Oct 19[, 2026]
    r"|/?\b(?P<od>\d{1,2})(?:st|nd|rd|th)\b",                           # 19th, /21st
    re.I)
_TIME_TOKEN = re.compile(
    r"\b\d{1,2}:\d{2}\b"                                      # 8:00, 08:00
    r"|\b\d{1,4}\s*(?:a\.?m\.?|p\.?m\.?)(?![a-z])",           # 8 AM, 0800AM, 1556PM
    re.I)
_MILITARY_TIME = re.compile(r"\b(?:[01]\d|2[0-3])[0-5]\d\b")  # 1225 (in "1225/12th")


def quote_dates(quote: str) -> list:
    """[(month or None, day)] for every date a quote states."""
    out = []
    for m in _DATE_TOKEN.finditer(quote or ""):
        if m.group("iy"):
            out.append((int(m.group("im")), int(m.group("id"))))
        elif m.group("m"):
            out.append((int(m.group("m")), int(m.group("d"))))
        elif m.group("mn"):
            out.append((_MONTHS.index(m.group("mn").lower()[:3]) + 1, int(m.group("md"))))
        elif m.group("od"):
            out.append((None, int(m.group("od"))))
    return [(mo, d) for mo, d in out if 1 <= d <= 31 and (mo is None or 1 <= mo <= 12)]


def quote_has_time(quote: str) -> bool:
    """Does the quote state a clock time? "8:00", "8 AM", "0800AM/19th",
    "1556PM/13th", or a bare 24-hour "1225" (Belinda's "1225/12th") when the
    quote also states a date -- a bare 4-digit number alone isn't trusted."""
    q = quote or ""
    has_date = bool(_DATE_TOKEN.search(q))
    rest = _DATE_TOKEN.sub(" ", q)
    if _TIME_TOKEN.search(rest):
        return True
    return has_date and bool(_MILITARY_TIME.search(rest))


def _last_delivered_quote(rec: dict) -> str:
    ev = next((e for e in reversed(rec.get("events") or [])
               if e.get("event") == "delivered"), {})
    return str((ev.get("evidence") or {}).get("quote") or "")


def delivered_time_known(rec: dict) -> bool:
    """Is the record's delivered_at time real (stated by Lineage / the DC)?
    The stored flag wins; records from before 2026-10-22 have none, so it is
    read off their delivered quote (no migration)."""
    raw = str((rec or {}).get("delivered_at") or "")
    if not re.search(r"T\d{2}:\d{2}", raw):
        return False                       # a bare date has no time to know
    flag = (rec or {}).get("delivered_time_known")
    if isinstance(flag, bool):
        return flag
    return quote_has_time(_last_delivered_quote(rec or {}))


def delivered_local(rec: dict) -> str:
    """The dock's wall-clock delivery time -- or, when Lineage gave only a
    date, that date at 00:00 (shown without a time)."""
    local = wall_time((rec or {}).get("delivered_at") or "")
    if local and not delivered_time_known(rec):
        return local[:10] + "T00:00:00"
    return local


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
    rec_in = rec
    rec = dict(rec) if rec else new_record(po)
    rec.setdefault("events", [])
    rec.setdefault("chases", [])
    old = {k: rec.get(k, "") for k in
           ("status", "delivery_appt", "pickup_at", "delivered_at", "confirmation_no",
            "delivered_time_known")}

    if kind == "picked_up" and not rec_in:
        return rec_in, None, ("PO isn't tracked yet -- a pickup is recorded on a PO "
                              "Lineage has a request, schedule or delivery for")
    if kind in ("scheduled", "rescheduled", "picked_up", "delivered"):
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
    elif kind == "picked_up":
        # When the truck actually left Woodside (Lineage: "picked up
        # 1556PM/13th"). Doesn't change status; it is the start of the
        # lead-time measurement (integrations/lead_times.py).
        at = parse_dt(ev.get("pickup_at") or "")
        if at is None:
            return rec, None, "pickup_at must be an ISO date/datetime"
        if at > now + DELIVERED_FUTURE_SLACK:
            return rec, None, f"pickup_at {ev.get('pickup_at')} is in the future"
        dl = parse_dt(rec.get("delivered_at") or "")
        if dl and at > dl:
            return rec, None, "pickup_at is after the confirmed delivery"
        rec["pickup_at"] = str(ev["pickup_at"]).strip()
    elif kind == "delivered":
        at = parse_dt(ev.get("delivered_at") or "")
        if at is None:
            return rec, None, "delivered_at must be an ISO date/datetime"
        if at > now + DELIVERED_FUTURE_SLACK:
            return rec, None, f"delivered_at {ev.get('delivered_at')} is in the future"
        req = parse_dt(rec.get("requested_at") or "")
        if req and at < req - timedelta(days=1):
            return rec, None, "delivered_at is before the PO was even sent to Lineage"
        # An operator Reopen undoes a confirmation; the cloud task re-reads the
        # same thread twice a day and would re-post the line it came from,
        # silently undoing the Reopen. Only a NEW message can re-confirm.
        evs = list(rec.get("events") or [])
        last_reopen = max((i for i, e in enumerate(evs)
                           if e.get("event") == "reopened"), default=-1)
        reopened_ids = {(e.get("evidence") or {}).get("message_id")
                        for e in evs[:max(last_reopen, 0)]
                        if e.get("event") == "delivered"} - {"", None}
        if (last_reopen >= 0 and evid["message_id"] in reopened_ids
                and not ev.get("force")):
            return rec, None, ("an operator reopened this PO after that confirmation "
                               "-- only a newer message can confirm it again")
        # The date posted must be a date the quote states (when it states
        # any): the evidence check proves the line exists, not that the event
        # carries its date.
        local_day = wall_time(str(ev["delivered_at"]))[:10]
        qd = quote_dates(evid["quote"])
        if qd and local_day:
            mo, dy = int(local_day[5:7]), int(local_day[8:10])
            if not any(d == dy and (m is None or m == mo) for m, d in qd):
                said = ", ".join(f"{m}/{d}" if m else f"day {d}" for m, d in qd)
                return rec, None, (f"delivered_at is {mo}/{dy} but the quote says "
                                   f"{said} -- post the date the quote gives")
        # Is the TIME real? An explicit time_known wins; otherwise the quote
        # must state one ("0800AM/19th"). A date-only confirmation keeps its
        # day but never acts as an exact time (see delivered_time_known).
        tk = ev.get("time_known")
        if not isinstance(tk, bool):
            tk = quote_has_time(evid["quote"])
        rec["status"] = "delivered"
        rec["delivered_at"] = str(ev["delivered_at"]).strip()
        rec["delivered_time_known"] = bool(tk) and bool(
            re.search(r"T\d{2}:\d{2}", rec["delivered_at"]))

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


def record_chase(rec: dict, ask: str, now: datetime, message_id: str = "",
                 send_state: str = "") -> dict:
    """send_state (server-sent follow-ups): "claimed" before the draft exists,
    "draft" once createReply made it, "sent" after Graph accepted the send.
    Any of them counts as chased (REPEAT_ASK_AFTER), so a send that times out
    after the draft exists is never re-sent within 16 h."""
    rec = dict(rec)
    chase = {"at": now.isoformat(), "ask": ask, "message_id": message_id}
    if send_state:
        chase["send_state"] = send_state
    rec["chases"] = (list(rec.get("chases") or []) + [chase])[-40:]
    return rec


def reopen_delivered(rec: dict, now: datetime, note: str = "") -> dict:
    """An operator reopened a PO whose delivery was confirmed: undo the
    confirmation so the rollover gate HOLDS the PO again (Pending POs Reopen;
    before this, the restored lines rolled straight back on the next read).

    Status goes back to "scheduled" if an appointment was on file, else
    "requested"; delivered_at is cleared; the old confirmation is kept in the
    history entry so it can be audited. ``now`` is aware UTC. No-op unless the
    record is "delivered".
    """
    rec = dict(rec or {})
    if rec.get("status") != "delivered":
        return rec
    new_status = "scheduled" if rec.get("delivery_appt") else "requested"
    entry = {"event": "reopened", "by": "operator", "at": now.isoformat(),
             "status": new_status, "delivered_at": "",
             "reopened_delivered_at": rec.get("delivered_at") or "",
             "note": str(note or "")[:300],
             "evidence": {"message_id": "", "sender": "", "sent": "", "quote": ""}}
    rec["status"] = new_status
    rec["delivered_at"] = ""
    rec["delivered_time_known"] = False
    rec["events"] = (list(rec.get("events") or []) + [entry])[-60:]
    rec["updated"] = now.isoformat()
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


# Where a reply's own text ends and the quoted history begins.
_HISTORY_START = re.compile(
    r"^\s*(?:From:|-{2,}\s*Original Message|_{8,}|On\b[^\n]*(?:\n[^\n]*)?\bwrote:)",
    re.I | re.M)


def own_text(text: str) -> str:
    """A message's own words, above any quoted history ("From:", "-----Original
    Message", "________________", "On ... wrote:")."""
    text = text or ""
    m = _HISTORY_START.search(text)
    return text[:m.start()] if m else text


def names_po(text: str, po_key: str) -> bool:
    """Does ``text`` name the PO (as a whole token -- "PO # 393072B2",
    "1209205-shows delivered", "HHB-1209205")?"""
    p = norm_po(po_key)
    return bool(p) and bool(re.search(r"(?<![A-Z0-9])" + re.escape(p) + r"(?![A-Z0-9])",
                                      (text or "").upper()))


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
        "delivered_local": delivered_local(rec),
        "delivered_time_known": (delivered_time_known(rec)
                                 if rec.get("delivered_at") else None),
        "closed_reason": rec.get("closed_reason") or "",
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
