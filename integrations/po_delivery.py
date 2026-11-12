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
Only an operator marking the PO Arrived (with a date) on the Pending tab
overrides the hold (arrival_confirmed); an expected arrival date typed there
is an estimate and the PO stays held until Lineage / the DC confirms.
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
ANSWERED_HOLD_FOR = timedelta(hours=48)  # Lineage replied since our last ask: don't re-ask
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
    if ev.get("source"):
        entry["source"] = str(ev["source"])[:40]     # "server" = read by the app itself
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
            "\n\nIf any delivery date changes, please let us know on this thread.\n"
            + PO_REPLY_ASK + "\n\n" + SIGNATURE)


# JD 11/11: on a thread about several POs, "This order is set for delivery at
# 11/13 @0900AM" doesn't say which order -- ask for the PO # on every chase.
PO_REPLY_ASK = ("Please include the PO # in your reply so we can match each "
                "date to the right order.")


# ---------------------------------------------------------------------------
# Mailbox labels (JD 11/11): an Outlook category per message naming the PO(s)
# it is about -- "PO 7135734C \u00b7 USF La Mirada" -- so the multi-PO Quote and
# Schedule threads can be read and filtered by order. The subject is never
# changed: a new subject starts a new Exchange conversation, splitting the
# thread for everyone on it and hiding the fork from the sync.
# ---------------------------------------------------------------------------

NEEDS_REVIEW_LABEL = "PO ? - needs review"
_DIST_SHORT = (("us foods", "USF"), ("usf", "USF"), ("cheney", "Cheney"),
               ("chef", "CW"))
_STATE_ZIP = re.compile(r"^[A-Z]{2}\.?(?:\s+\d{5}(?:-\d{4})?)?$")


def short_place(warehouse: str = "", address: str = "") -> str:
    """'La Mirada, CA' -> 'La Mirada'; else the city out of an address
    ('US Foods, 15155 Northam Street, La Mirada, CA 90638' -> 'La Mirada',
    '..., Ocala FL 34475' -> 'Ocala')."""
    w = (warehouse or "").split(",")[0].strip()
    if w:
        return w
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    for i in range(len(parts) - 1, 0, -1):
        if _STATE_ZIP.match(parts[i].upper()):
            return parts[i - 1]
        m = re.match(r"^(.*?[A-Za-z])\s+[A-Z]{2}\.?\s+\d{5}", parts[i])
        if m and not re.search(r"\d", m.group(1)):
            return m.group(1).strip()
    return ""


def po_label(po_number: str, distributor: str = "", place: str = "") -> str:
    d = (distributor or "").lower()
    short = next((s for k, s in _DIST_SHORT if k in d), "")
    where = " ".join(x for x in (short, place) if x)
    return f"PO {po_number} \u00b7 {where}" if where else f"PO {po_number}"


_DATE_CUE = re.compile(
    r"\b(?:deliver\w*|pick(?:ed)?\s*-?up|arriv\w*|appointment|appt|scheduled|"
    r"rescheduled|set\s+for|booked|ETA)\b", re.I)


def date_lines(text: str) -> list:
    """Lines in a message's own words that put a date on a delivery / pickup /
    appointment -- firm or not ("This order is set for delivery at 11/13
    @0900AM", "This was picked up 11/03"). Used to flag a Lineage message
    that carries a date but names no PO, for a person to match."""
    out = []
    for raw in re.split(r"[\n]|(?<=[.!?])\s+(?=[A-Z])", own_text(text or "")):
        line = raw.strip().strip(".!").strip()
        if line and len(line) <= 220 and _DATE_CUE.search(line) and quote_dates(line):
            out.append(line)
    return out


_REQUEST = re.compile(
    r"\?|\b(?:please|pls|need|send|confirm|update|advise|let\s+(?:us|me)\s+know|"
    r"can\s+you|could\s+you|when|where|status|eta|escalat\w*)\b", re.I)


def is_ack(text: str) -> bool:
    """A short H&H note that asks nothing ("Thanks so much for getting us the
    info") -- not the message a following Lineage reply is answering."""
    own = own_text(text or "").strip()
    return len(own) < 400 and not _REQUEST.search(own)


INTERNAL_TEST_PREFIX = "TEST (internal"   # H&H-only test replies: never "activity"


# Out-of-office / auto-replies are never an answer and never "activity"
# (Belinda's 11/6 OOO landed seconds after a chase she had already answered).
_AUTO_REPLY_SUBJECT = re.compile(
    r"^\s*(?:automatic\s+reply|auto(?:matic)?[- ]?reply|out\s+of\s+(?:the\s+)?office)\b",
    re.I)
_AUTO_REPLY_TEXT = re.compile(
    r"\b(?:out\s+of\s+(?:the\s+)?office|I\s+will\s+(?:be\s+)?return(?:ing)?|"
    r"I\s+am\s+(?:currently\s+)?away|on\s+vacation|limited\s+access\s+to\s+(?:my\s+)?e-?mail)\b",
    re.I)


def is_auto_reply(msg: dict) -> bool:
    """An automatic / out-of-office reply (by subject, or a short body that
    reads like one)."""
    if _AUTO_REPLY_SUBJECT.search(msg.get("subject") or ""):
        return True
    own = own_text(msg.get("text") or "").strip()
    return len(own) < 600 and bool(_AUTO_REPLY_TEXT.search(own))


# A reply naming no PO that answers an H&H message about SEVERAL POs speaks for
# all of them only when it says so: "Both shows delivered today" (Belinda,
# 11/5, 5-Orders thread, answering the chase for 014511767346 + 054511767347).
_COLLECTIVE = re.compile(r"\b(?:both|all|each|every|these|those|they)\b", re.I)


def speaks_for_all(text: str) -> bool:
    return bool(_COLLECTIVE.search(own_text(text or "")))


def asked_in_answered(msg: dict, messages: list, po_numbers: list) -> list:
    """The POs asked about in the H&H message ``msg`` answers: the latest H&H
    message on the thread before it, passing over internal test notes and a
    bare "thanks" that names no PO. [] when there is none or it names no PO."""
    sent = parse_dt(msg.get("sent") or "")
    if sent is None:
        return []
    earlier = []
    for m in messages:
        t = parse_dt(m.get("sent") or "")
        if t is None or t >= sent or not is_hh(m.get("from") or ""):
            continue
        if (m.get("text") or "").lstrip().startswith(INTERNAL_TEST_PREFIX):
            continue
        earlier.append((t, own_text(m.get("text") or "")))
    for _t, own in sorted(earlier, key=lambda x: x[0], reverse=True):
        if is_ack(own) and not any(names_po(own, p) for p in po_numbers):
            continue
        return [p for p in po_numbers if names_po(own, p)]
    return []


def answered_since_chase(rec: dict, messages: list, po_numbers: list,
                         now: datetime) -> Optional[str]:
    """Did Lineage / the dock reply on this thread AFTER our last follow-up
    about this PO (and within ANSWERED_HOLD_FOR)? Then the ball is in our
    court -- the reply needs reading, not another ask. Returns the hold
    reason, or None. A reply naming other POs only doesn't count. One naming
    none counts for the POs the H&H message it answers asked about -- or,
    when that message named none (or there is none), for every PO on the
    thread. Auto-replies never count.

    Before this, a reply the server couldn't read ("Both shows delivered
    today") was invisible to the chase rule, so the next run asked again.

    11/9 21:10 UTC, 8-Orders thread: Belinda's "Reaching out to dispatch to
    confirm." answered JD's ask about 1226134 alone, but (naming no PO) it
    held 064511780513, 1220440 and 9314683Y for the full 48 h -- two missed
    days of follow-ups on two passed appointments."""
    last = _last_chase(rec)
    if last is None:
        return None
    key = norm_po(rec.get("po_number") or "")
    pos = [norm_po(p) for p in po_numbers]
    for m in sorted(messages, key=lambda m: m.get("sent") or "", reverse=True):
        sent = parse_dt(m.get("sent") or "")
        if sent is None or sent <= last or now - sent >= ANSWERED_HOLD_FOR:
            continue
        if not sender_may_report(m.get("from") or "") or is_auto_reply(m):
            continue
        named = [p for p in pos if names_po(own_text(m.get("text") or ""), p)]
        if named and key not in named:
            continue
        if not named:
            asked = asked_in_answered(m, messages, pos)
            if asked and key not in asked:
                continue      # it answered an ask about other POs
        return (f"{m.get('from')} replied {sent.strftime('%m/%d %H:%M')} UTC after the "
                "last follow-up -- that reply needs reading, not another ask")
    return None


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
        if text.lstrip().startswith(INTERNAL_TEST_PREFIX) or is_auto_reply(m):
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


# ---------------------------------------------------------------------------
# Reading a delivery confirmation on the SERVER (no scheduled task needed)
# ---------------------------------------------------------------------------
# Until 11/4 only the twice-a-weekday cloud task read Lineage's replies, so a
# confirmation that landed at 10:30 AM sat until 3:22 PM (a Friday-evening
# one until Monday), and one the task misread never landed at all: 7728445G
# (Zebulon) showed "Awaiting delivery confirmation" for two days after
# Belinda's 11/2 detention notice gave its arrival and departure times, and
# the server kept asking her about it. The server now reads the plain,
# unambiguous confirmations itself every time it syncs the threads (every
# 20 minutes, scheduler.py). Anything hedged, partial or about several POs
# at once is left for the task / a person -- a wrong "delivered" releases a
# PO into on-hand, a missed one only waits for the next read.

_CONFIRM = re.compile(r"\bdelivered\b", re.I)
# Words that make a "delivered" line a question, a plan or a problem.
_HEDGE = re.compile(
    r"n't\b|\?|\b(?:not|never|no|if|whether|confirm|confirming|can|could|would|"
    r"please|will|to\s+be|waiting|pending|yet|missed|reschedul\w*|attempt\w*|"
    r"expected|should|eta|when|once|until|before|tomorrow|tonight)\b", re.I)
# Words that put the whole message beyond a plain confirmation.
_PROBLEM = re.compile(
    r"\b(?:partial\w*|short\w*|damag\w*|refus\w*|reject\w*|overage|missing|"
    r"not\s+delivered|undeliver\w*|returned|claim)\b", re.I)
_ARRIVAL = re.compile(r"^\s*Arrival\s+Time\s*:\s*(\S.*?)\s*$", re.I | re.M)
_DEPARTURE = re.compile(r"^\s*Departure\s+Time\s*:\s*\S", re.I | re.M)
_SENTENCE = re.compile(r"[^\n.!?]+(?:[.!](?!\d)|$)", re.M)
# A line that opens with its own PO number ("9314683Y-had to reach out ...",
# "PO 1220440: shows delivered") speaks for that PO alone.
_PO_LED = re.compile(r"^\s*(?:PO\s*#?\s*:?\s*)?(?=[A-Z0-9]*\d)[A-Z0-9]{5,16}\s*[-\u2013\u2014:]",
                     re.I)

_STATE_ZONES = {
    **{s: "America/New_York" for s in ("NY", "NJ", "CT", "MA", "PA", "MD", "DE",
                                        "DC", "VA", "NC", "SC", "GA", "FL", "OH",
                                        "MI", "IN")},
    **{s: "America/Chicago" for s in ("IL", "TX", "TN", "WI", "MN", "MO", "AL",
                                       "MS", "LA", "AR", "OK", "KS", "IA")},
    **{s: "America/Denver" for s in ("CO", "UT", "NM", "AZ")},
    **{s: "America/Los_Angeles" for s in ("CA", "WA", "OR", "NV")},
}


def dock_zone(*places: str) -> str:
    """IANA zone of the receiving dock from an address or a "City, ST"
    warehouse label; Eastern when nothing says."""
    for p in places:
        for st in re.findall(r"\b([A-Z]{2})\b\.?\s*(?:\d{5})?\s*$|,\s*([A-Z]{2})\b",
                             str(p or "")):
            for s in st:
                if s in _STATE_ZONES:
                    return _STATE_ZONES[s]
    return "America/New_York"


def _clock(quote: str) -> Optional[tuple]:
    """(hour, minute) a confirmation line states, or None. "0800AM", "543AM",
    "8:00 AM", "1556PM", "1225" (24-hour, only next to a date)."""
    rest = _DATE_TOKEN.sub(" ", quote or "")
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?(?![a-z])", rest, re.I) \
        or re.search(r"\b(\d{1,4})\s*(a\.?m\.?|p\.?m\.?)(?![a-z])", rest, re.I)
    if m and ":" in m.group(0):
        h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "")
    elif m:
        digits, ap = m.group(1), m.group(2)
        h, mi = (int(digits), 0) if len(digits) <= 2 else (int(digits[:-2]), int(digits[-2:]))
    else:
        mm = _MILITARY_TIME.search(rest) if _DATE_TOKEN.search(quote or "") else None
        if not mm:
            return None
        h, mi, ap = int(mm.group(0)[:2]), int(mm.group(0)[2:]), ""
    ap = ap.lower().replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return (h, mi) if 0 <= h <= 23 and 0 <= mi <= 59 else None


def _day(quote: str, sent_local: datetime) -> Optional[datetime]:
    """The (naive) calendar day a line states, resolved against the day the
    message was sent: "2nd" -> this month or last; "11/2" -> this year or last.
    Never later than the send day."""
    qd = quote_dates(quote)
    if not qd:
        # "delivered today" / "delivered yesterday": relative to the send day
        if re.search(r"\btoday\b", quote or "", re.I):
            return datetime(sent_local.year, sent_local.month, sent_local.day)
        if re.search(r"\byesterday\b", quote or "", re.I):
            return (datetime(sent_local.year, sent_local.month, sent_local.day)
                    - timedelta(days=1))
        return None
    mo, d = qd[0]
    y = sent_local.year
    if mo is None:
        mo = sent_local.month
        if d > sent_local.day:
            mo -= 1
            if mo == 0:
                mo, y = 12, y - 1
    try:
        day = datetime(y, mo, d)
    except ValueError:
        return None
    if day.date() > sent_local.date():
        try:
            day = datetime(y - 1, mo, d)
        except ValueError:
            return None
    return day


def confirmation_lines(text: str) -> list:
    """Plain delivery confirmations in a message's own words, as (line, kind):
    "delivered" for "This shows delivered 0900AM/22nd" / "7236706H-delivered"
    / "This load delivered 11/2"; "arrival" for a detention notice's
    "Arrival Time: 543AM/2nd" (it only exists once the truck unloaded, and
    only counts next to a Departure time). Empty when the message hedges or
    reports a problem anywhere.

    A hedged line that opens with its own PO number holds back only that PO
    (Belinda 11/12: "064511780513-shows delivered / 1220440-shows delivered /
    9314683Y-had to reach out to dispatch to confirm if delivered" -- the
    third line used to discard the first two). The other lines then count
    only when they, too, name their PO."""
    own = own_text(text or "")
    if _PROBLEM.search(own):
        return []
    out = []
    scoped_hedge = False
    for raw in _SENTENCE.findall(own):
        line = raw.strip().strip(".!").strip()
        if not line or not _CONFIRM.search(line):
            continue
        if _HEDGE.search(line):
            if _PO_LED.match(line):
                scoped_hedge = True   # that PO is left to a reader; the rest stand
                continue
            return []            # one hedged "delivered" line: leave it to a reader
        out.append((line, "delivered"))
    if scoped_hedge:
        out = [(ln, k) for ln, k in out if _PO_LED.match(ln)]
    if _DEPARTURE.search(own):
        for m in _ARRIVAL.finditer(own):
            line = m.group(0).strip()
            if quote_dates(line):
                out.append((line, "arrival"))
    return out


def delivered_at_from(line: str, sent: str, zone: str,
                      appt: str = "") -> Optional[tuple]:
    """(delivered_at ISO, time_known) for a confirmation line, or None.
    A stated day (+ time when given, in the dock's zone); no day -> the
    appointment's day, if the appointment had passed when the line was sent."""
    from zoneinfo import ZoneInfo
    sent_dt = parse_dt(sent)
    if sent_dt is None:
        return None
    tz = ZoneInfo(zone)
    sent_local = sent_dt.astimezone(tz)
    line = delivery_part(line) or line   # "picked up 1556PM/13th, delivered 0651AM/21st"
    day = _day(line, sent_local)
    if day is None:
        a = parse_dt(appt)
        if a is None or a > sent_dt:
            return None
        return a.astimezone(tz).strftime("%Y-%m-%d"), False
    clk = _clock(line)
    if clk is None:
        return day.strftime("%Y-%m-%d"), False
    local = day.replace(hour=clk[0], minute=clk[1]).replace(tzinfo=tz)
    if local > sent_local + DELIVERED_FUTURE_SLACK:
        return None
    return local.isoformat(timespec="seconds"), True


# ---------------------------------------------------------------------------
# Appointment changes the server reads itself (2026-11-10)
# ---------------------------------------------------------------------------
# JD: "I want these updated arrival dates to automatically get updated in the
# dashboard as they are sent." Miss that prompted it: Cheney's Marbel Fox wrote
# "We show this PO scheduled for 11/11 at 8:30 AM" on a one-off
# "PO 064511780513 - Delivery appointment help" thread; the Pending tab kept
# Lineage's earlier 11/18 because only the twice-daily task read schedules,
# and it only reads the Quote and Schedule threads. Like confirmation_lines,
# only FIRM statements are read: a request, a question or a conditional
# ("if you are able ... otherwise 11/18") is left for a person -- a wrong
# appointment date is cosmetic (the PO stays held either way), a guessed one
# from a conditional is not worth showing.

_APPT_CUE = re.compile(
    r"\b(?:scheduled|rescheduled|appointment|appt|moved\s+to|pushed\s+to|"
    r"set\s+for|booked|will\s+(?:now\s+)?deliver|delivering|deliver(?:s|y)?\s+(?:on|for))\b",
    re.I)
_APPT_HEDGE = re.compile(
    r"\?|n't\b|\b(?:if|unless|otherwise|whether|request\w*|ask\w*|can|could|"
    r"would|please|try\w*|hop\w*|possible|possibly|tentative\w*|pending|maybe|"
    r"not|no|need\w*|want\w*|prefer\w*|instead\s+of|was\s+given|received|"
    r"capacity|delivered|picked)\b", re.I)


_PICKUP_CUE = re.compile(r"\bpick(?:ed|ing)?[\s-]?up", re.I)
_DELIVERY_WORD = re.compile(r"\bdeliver\w*", re.I)


def delivery_part(line: str) -> str:
    """The part of a line that is about the DELIVERY. Belinda puts both on one
    line -- "014511780511-pickup 1100AM/16th and delivery is set for
    1000AM/18th" -- and the first date / time there is the pickup (11/11: the
    server booked the 11/16 11:00 pickup as the delivery appointment, and
    re-booked it on every sync after the task corrected it). A line with no
    pickup cue is all delivery; one with a pickup cue is read from its first
    "deliver..." word up to the next pickup cue; "" when it has no delivery
    word at all (a pickup time is not a delivery appointment)."""
    line = line or ""
    if not _PICKUP_CUE.search(line):
        return line
    d = _DELIVERY_WORD.search(line)
    if not d:
        return ""
    p = _PICKUP_CUE.search(line, d.end())
    return line[d.start():p.start() if p else len(line)]


def appointment_lines(text: str) -> list:
    """Firm delivery-appointment statements in a message's own words:
    "We show this PO scheduled for 11/11 at 8:30 AM", "Appointment: 10/19/2026
    8:00 AM", "Delivery moved to 0830AM/18th". Each line must state a day
    (in its delivery part -- see delivery_part). A hedged / conditional /
    requesting line is skipped, and a message whose firm lines disagree on the
    day is skipped whole (ambiguous)."""
    own = own_text(text or "")
    if _PROBLEM.search(own):
        return []
    out = []
    for raw in re.split(r"[\n]|(?<=[.!?])\s+(?=[A-Z])", own):
        line = raw.strip().strip(".!").strip()
        if not line or len(line) > 220:
            continue
        if not (_APPT_CUE.search(line) and quote_dates(delivery_part(line))):
            continue
        if _APPT_HEDGE.search(line):
            continue
        out.append(line)
    days = {quote_dates(delivery_part(l))[0] for l in out}
    return out if len(days) == 1 else []


def appointment_from(line: str, sent: str, zone: str,
                     current_appt: str = "") -> Optional[str]:
    """ISO appointment (dock zone offset) for an appointment line, or None.
    The day is resolved FORWARD from the send day ("18th" sent 11/9 -> 11/18;
    "1/4" sent 12/28 -> next January). With no time stated, the current
    appointment's time is kept; with neither, nothing is read."""
    from zoneinfo import ZoneInfo
    sent_dt = parse_dt(sent)
    if sent_dt is None:
        return None
    tz = ZoneInfo(zone)
    s = sent_dt.astimezone(tz)
    line = delivery_part(line)       # never the pickup's day / time
    qd = quote_dates(line)
    if not qd:
        return None
    mo, d = qd[0]
    y = s.year
    if mo is None:
        mo = s.month
        if d < s.day:
            mo += 1
            if mo == 13:
                mo, y = 1, y + 1
    elif (mo, d) < (s.month, s.day) and (s.month - mo) % 12 > 2:
        y += 1                       # "1/4" written in late December
    try:
        day = datetime(y, mo, d)
    except ValueError:
        return None
    clk = _clock(line)
    if clk is None:
        cur = parse_dt(current_appt)
        if cur is None:
            return None
        cur = cur.astimezone(tz)
        clk = (cur.hour, cur.minute)
    local = day.replace(hour=clk[0], minute=clk[1]).replace(tzinfo=tz)
    if local < s - timedelta(days=1):
        return None                  # a past day isn't a new appointment
    return local.isoformat(timespec="seconds")
