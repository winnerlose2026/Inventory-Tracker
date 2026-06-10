"""Weekly inventory-report status — who has / hasn't sent this week's report.

Powers the cloud-independent status page (``/report-status``) so JD can see,
from any device, which distributor warehouses have sent their weekly bagel
inventory & usage report and which are still outstanding — without the Cowork
desktop app running.

Detection mirrors the chaser logic: the WAREHOUSE is the unit, not the person.
A warehouse counts as reported if ANY of its covering reps sent a *genuine*
inventory/usage report since the most recent Friday. "Genuine" means the email
carries a real (non-inline) spreadsheet/CSV attachment OR its body is an actual
multi-variety data table — NOT a signature-logo image or a prose email that
merely mentions the words "inventory"/"bagels" (e.g. an "out of stock"
escalation thread). Cheney's three FL facilities clear off Michael Ross's
single combined report.

Read-only: uses the same MS365 client-credentials (Mail.Read) the rest of the
app already uses; it sends nothing. All times are handled in UTC; the page
renders an approximate US-Eastern (EDT) label for readability.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import json as _json
import os as _os
import pathlib as _pathlib
import re as _re
import time as _time
import urllib.error as _uerr
import urllib.parse as _url
import urllib.request as _ureq
from html.parser import HTMLParser as _HTMLParser

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Warehouse -> covering reps. Warehouse labels match seed_bagels.py exactly.
# Multiple reps can cover one warehouse; any one of them sending a genuine
# report clears it. Cheney: Michael Ross sends ONE report (per-facility sheets)
# covering all three FL facilities, so his address is listed against each.
WAREHOUSE_REPS: "dict[str, list[dict]]" = {
    "Manassas, VA": [
        {"name": "Thomas Paxson", "email": "thomas.paxson@usfoods.com"},
        {"name": "Jasmin Gomez", "email": "jasmin.gomez@usfoods.com"},
        {"name": "USF Manassas coordination", "email": "5o-dl-streetsalescoordination@usfoods.com"},
    ],
    "Zebulon, NC": [
        {"name": "Maria Hernandez", "email": "maria.hernandez@usfoods.com"},
        {"name": "Kathleen Thompson", "email": "kathleen.thompson@usfoods.com"},
    ],
    "La Mirada, CA": [
        {"name": "Sam Travlos", "email": "sam.travlos@usfoods.com"},
        {"name": "Ozzy Corut", "email": "ozzy.corut@usfoods.com"},
    ],
    "Chicago, IL": [
        {"name": "Michael Via", "email": "michael.via@usfoods.com"},
    ],
    "Alcoa, TN": [
        {"name": "Kimberly Cobb", "email": "kimberly.cobb@usfoods.com"},
        {"name": "Christy Dunn", "email": "christy.dunn@usfoods.com"},
    ],
    "Riviera Beach, FL": [{"name": "Michael Ross", "email": "mross@cheneybrothers.com"}],
    "Ocala, FL": [{"name": "Michael Ross", "email": "mross@cheneybrothers.com"}],
    "Punta Gorda, FL": [{"name": "Michael Ross", "email": "mross@cheneybrothers.com"}],
}

DISTRIBUTOR_OF = {
    "Manassas, VA": "US Foods", "Zebulon, NC": "US Foods", "La Mirada, CA": "US Foods",
    "Chicago, IL": "US Foods", "Alcoa, TN": "US Foods",
    "Riviera Beach, FL": "Cheney Brothers", "Ocala, FL": "Cheney Brothers",
    "Punta Gorda, FL": "Cheney Brothers",
}

# A genuine body report lists many SKUs. Count distinct variety names; a real
# report hits the whole order guide, while a prose "out of Everything & Plain"
# escalation hits only one or two. Require at least 3 distinct varieties.
_VARIETY_TOKENS = ("plain", "everything", "sesame", "poppy", "onion", "asiago",
                   "cinnamon raisin", "jalapeno", "blueberry", "egg",
                   "whole wheat", "pumpernickel", "salt", "garlic")
_MIN_VARIETIES = 3

_AUTO_REPLY_MARKERS = ("automatic reply", "out of office", "out-of-office",
                       "undeliverable", "delivery has failed", "delivery failure")
# Subjects that are clearly not the weekly report; block body-only acceptance.
_BLOCK_SUBJECT = ("out of bagel", "past due", "appointment", "forgery",
                  "statement", "affidavit", "invoice")
_DATA_EXT = (".xlsx", ".xls", ".csv")

_CACHE = {"at": 0.0, "data": None}


def most_recent_friday(now: _dt.datetime) -> _dt.datetime:
    """Midnight (UTC) of the most recent Friday on or before ``now``."""
    days_since_fri = (now.weekday() - 4) % 7  # Mon=0 .. Fri=4 .. Sun=6
    fri = now - _dt.timedelta(days=days_since_fri)
    return fri.replace(hour=0, minute=0, second=0, microsecond=0)


def _email_to_name() -> "dict[str, str]":
    return {rep["email"].lower(): rep["name"]
            for reps in WAREHOUSE_REPS.values() for rep in reps}


def _rep_to_warehouses() -> "dict[str, list[str]]":
    out: "dict[str, list[str]]" = {}
    for wh, reps in WAREHOUSE_REPS.items():
        for rep in reps:
            out.setdefault(rep["email"].lower(), []).append(wh)
    return out


def _mailboxes() -> "list[str]":
    raw = _os.environ.get("MAILBOXES") or _os.environ.get("MS365_USER") or ""
    return [m.strip() for m in raw.split(",") if m.strip()]


def _token() -> str:
    tenant = _os.environ["MS365_TENANT_ID"]
    data = _url.urlencode({
        "client_id": _os.environ["MS365_CLIENT_ID"],
        "client_secret": _os.environ["MS365_CLIENT_SECRET"],
        "scope": GRAPH_SCOPE,
        "grant_type": "client_credentials",
    }).encode("utf-8")
    url = f"https://login.microsoftonline.com/{_url.quote(tenant)}/oauth2/v2.0/token"
    req = _ureq.Request(url, data=data,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        method="POST")
    with _ureq.urlopen(req, timeout=30) as resp:
        return _json.loads(resp.read().decode("utf-8"))["access_token"]


def _graph_get(token: str, url: str) -> dict:
    req = _ureq.Request(url, headers={"Authorization": f"Bearer {token}",
                                      "Accept": "application/json"})
    with _ureq.urlopen(req, timeout=45) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _rep_messages(token: str, mailbox: str, rep_email: str, since_iso: str,
                  top: int = 10) -> list:
    """All messages from ``rep_email`` in ``mailbox`` since ``since_iso``,
    each with body + attachment metadata, in a single Graph call (no inbox
    paging). Returns the raw message dicts."""
    q = {
        "$select": "id,subject,from,receivedDateTime,hasAttachments,body",
        "$expand": "attachments($select=name,contentType,isInline,size)",
        "$top": str(top),
        "$filter": (f"from/emailAddress/address eq '{rep_email}' "
                    f"and receivedDateTime ge {since_iso}"),
    }
    url = f"{GRAPH_BASE}/users/{_url.quote(mailbox)}/messages?{_url.urlencode(q)}"
    return _graph_get(token, url).get("value", []) or []


_WS_RE = _re.compile(r"\s+")


class _HTMLTextExtractor(_HTMLParser):
    """Pull visible text out of an HTML email body, dropping <script>/<style>
    content. Uses a real HTML parser rather than a tag-stripping regex, so
    malformed or obfuscated markup can't slip through (CodeQL py/bad-tag-filter).
    The output is only used for keyword counting, never rendered."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _strip_html(body: dict) -> str:
    content = (body or {}).get("content") or ""
    if ((body or {}).get("contentType") or "").lower() == "html":
        parser = _HTMLTextExtractor()
        try:
            parser.feed(content)
            parser.close()
            content = parser.get_text()
        except Exception:
            content = ""
        content = _html.unescape(content)
    return _WS_RE.sub(" ", content).strip().lower()


def _variety_count(text_lower: str) -> int:
    return sum(1 for tok in _VARIETY_TOKENS if tok in text_lower)


def _has_data_attachment(msg: dict) -> bool:
    for att in msg.get("attachments", []) or []:
        if att.get("isInline"):
            continue
        name = (att.get("name") or "").lower()
        ctype = (att.get("contentType") or "").lower()
        if name.endswith(_DATA_EXT) or "spreadsheet" in ctype or ctype == "text/csv":
            return True
    return False


def _classify(msg: dict) -> "tuple[bool, str]":
    """Return (is_report, format_label) for a fully-fetched message."""
    subj = (msg.get("subject") or "").lower()
    if any(mk in subj for mk in _AUTO_REPLY_MARKERS):
        return False, ""
    if _has_data_attachment(msg):
        return True, "spreadsheet"
    if any(b in subj for b in _BLOCK_SUBJECT):
        return False, ""
    if _variety_count(_strip_html(msg.get("body") or {})) >= _MIN_VARIETIES:
        return True, "in email body"
    return False, ""


def compute_report_status(now: "_dt.datetime | None" = None) -> dict:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    since = most_recent_friday(now)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    mailboxes = _mailboxes()
    result = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "since_iso": since_iso,
        "mailboxes": mailboxes,
        "configured": bool(mailboxes and _os.environ.get("MS365_TENANT_ID")),
        "warehouses": [], "received": 0, "missing": 0, "scanned": 0, "error": None,
    }
    if not result["configured"]:
        result["error"] = ("MS365 not configured — set MS365_TENANT_ID/"
                           "MS365_CLIENT_ID/MS365_CLIENT_SECRET and MAILBOXES "
                           "(or MS365_USER).")
        return result
    try:
        token = _token()
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"Graph token error: {type(exc).__name__}: {exc}"
        return result

    rep_wh = _rep_to_warehouses()
    names = _email_to_name()
    best: "dict[str, dict]" = {}
    scanned = 0
    for rep_email, whs in rep_wh.items():
        found = None
        for mailbox in mailboxes:
            try:
                msgs = _rep_messages(token, mailbox, rep_email, since_iso)
            except Exception:  # noqa: BLE001
                continue  # a rep we can't query just stays unconfirmed (missing)
            for msg in msgs:
                scanned += 1
                is_rep, fmt = _classify(msg)
                if not is_rep:
                    continue
                rd = msg.get("receivedDateTime") or ""
                if found is None or rd > found["received_at"]:
                    found = {
                        "received_at": rd, "rep_name": names.get(rep_email, rep_email),
                        "rep_email": rep_email, "mailbox": mailbox,
                        "subject": msg.get("subject") or "", "format": fmt,
                    }
        if found:
            for wh in whs:
                cur = best.get(wh)
                if cur is None or found["received_at"] > cur["received_at"]:
                    best[wh] = found

    result["scanned"] = scanned
    for wh, reps in WAREHOUSE_REPS.items():
        detail = best.get(wh)
        result["warehouses"].append({
            "warehouse": wh, "distributor": DISTRIBUTOR_OF.get(wh, ""),
            "reps": reps, "status": "received" if detail else "missing",
            "detail": detail,
        })
        result["received" if detail else "missing"] += 1
    return result


def _status_path() -> "_pathlib.Path":
    try:
        from inventory_tracker import DATA_DIR  # type: ignore
        base = DATA_DIR
    except Exception:  # noqa: BLE001
        base = _pathlib.Path("data")
    return _pathlib.Path(base) / "report_status.json"


def _read_disk():
    try:
        path = _status_path()
        age = _time.time() - path.stat().st_mtime
        return _json.loads(path.read_text(encoding="utf-8")), age
    except Exception:  # noqa: BLE001
        return None, None


def _write_disk(data: dict) -> None:
    try:
        path = _status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def get_status(max_age_sec: int = 1800, force: bool = False) -> dict:
    """Layered cache: in-process memory -> shared on-disk snapshot
    (data/report_status.json) -> live recompute. ``force`` always recomputes."""
    now = _time.time()
    if not force:
        cached = _CACHE.get("data")
        if cached is not None and (now - _CACHE.get("at", 0.0)) < max_age_sec:
            out = dict(cached); out["cached"] = "memory"; return out
        disk, age = _read_disk()
        if disk is not None and age is not None and age < max_age_sec and not disk.get("error"):
            _CACHE.update(at=now, data=disk)
            out = dict(disk); out["cached"] = "disk"; return out
    try:
        data = compute_report_status()
    except Exception as exc:  # noqa: BLE001
        data = {"generated_at": "", "configured": False, "warehouses": [],
                "received": 0, "missing": 0, "scanned": 0,
                "error": f"{type(exc).__name__}: {exc}"}
    if not data.get("error"):
        _CACHE.update(at=now, data=data)
        _write_disk(data)
    out = dict(data); out["cached"] = False; return out


def _fmt_et(iso_z: str) -> str:
    try:
        d = (_dt.datetime.strptime(iso_z, "%Y-%m-%dT%H:%M:%SZ")
             .replace(tzinfo=_dt.timezone.utc))
    except Exception:  # noqa: BLE001
        return iso_z or ""
    et = d - _dt.timedelta(hours=4)  # approx US Eastern (EDT)
    try:
        return et.strftime("%a %b %-d, %-I:%M %p ET")
    except Exception:  # noqa: BLE001
        return et.strftime("%Y-%m-%d %H:%M ET")


def render_html(status: dict) -> str:
    esc = _html.escape
    gen_et = _fmt_et(status.get("generated_at") or "")
    since_et = _fmt_et(status.get("since_iso") or "")
    received = status.get("received", 0)
    missing = status.get("missing", 0)
    total = received + missing
    err = status.get("error")

    rows = []
    last_dist = None
    for wh in status.get("warehouses", []):
        dist = wh.get("distributor") or ""
        if dist != last_dist:
            rows.append(f'<tr class="grp"><td colspan="3">{esc(dist)}</td></tr>')
            last_dist = dist
        if wh.get("status") == "received":
            d = wh.get("detail") or {}
            badge = '<span class="ok">✅ received</span>'
            note = (f'from {esc(d.get("rep_name") or "")} · '
                    f'{esc(_fmt_et(d.get("received_at") or ""))} · '
                    f'{esc(d.get("format") or "report")}')
        else:
            badge = '<span class="no">❌ missing</span>'
            chasing = ", ".join(esc(r["name"]) for r in wh.get("reps", [])
                                if not r["name"].startswith("USF "))
            note = f'<span class="muted">chasing: {chasing}</span>'
        rows.append(f'<tr><td class="wh">{esc(wh.get("warehouse") or "")}</td>'
                    f'<td class="st">{badge}</td><td class="dt">{note}</td></tr>')
    rows_html = "\n".join(rows)

    if err:
        banner = f'<div class="err">⚠️ {esc(err)}</div>'
    elif total:
        tone = "allin" if missing == 0 else "pending"
        msg = ("All warehouses have reported this week 🎉" if missing == 0
               else f"{missing} of {total} warehouses still missing this week's report")
        banner = f'<div class="summary {tone}">{esc(msg)}</div>'
    else:
        banner = ""

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Weekly Report Status — H&amp;H Bagels</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 0; background: #f6f7f9; color: #1c1d21; }}
  .wrap {{ max-width: 720px; margin: 0 auto; padding: 18px 16px 48px; }}
  h1 {{ font-size: 20px; margin: 6px 0 2px; }}
  .sub {{ color: #6b7280; font-size: 13px; margin-bottom: 14px; }}
  .summary {{ padding: 12px 14px; border-radius: 10px; font-weight: 600; margin-bottom: 14px; }}
  .summary.pending {{ background: #fff4e5; color: #8a4b00; border: 1px solid #f3d9b5; }}
  .summary.allin {{ background: #e8f7ee; color: #1a6b39; border: 1px solid #bfe6cd; }}
  .err {{ background: #fdeaea; color: #9b1c1c; border: 1px solid #f3c1c1;
         padding: 12px 14px; border-radius: 10px; margin-bottom: 14px; font-size: 14px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
          border: 1px solid #e5e7eb; border-radius: 12px; overflow: hidden; }}
  td {{ padding: 11px 12px; border-top: 1px solid #eef0f2; font-size: 14px; vertical-align: top; }}
  tr.grp td {{ background: #f0f2f5; font-weight: 700; font-size: 12px; text-transform: uppercase;
              letter-spacing: .04em; color: #4b5563; border-top: none; }}
  td.wh {{ font-weight: 600; white-space: nowrap; }}
  td.st {{ white-space: nowrap; }}
  td.dt {{ color: #374151; }}
  .ok {{ color: #1a6b39; font-weight: 600; }}
  .no {{ color: #b42318; font-weight: 600; }}
  .muted {{ color: #6b7280; }}
  .foot {{ color: #9097a1; font-size: 12px; margin-top: 16px; line-height: 1.5; }}
  a.btn {{ display: inline-block; margin-top: 14px; padding: 9px 14px; background: #1c1d21;
          color: #fff; text-decoration: none; border-radius: 8px; font-size: 13px; }}
</style></head>
<body><div class="wrap">
  <h1>Weekly Inventory Report Status</h1>
  <div class="sub">Submissions since {esc(since_et)} · updated {esc(gen_et)}</div>
  {banner}
  <table><tbody>
  {rows_html}
  </tbody></table>
  <a class="btn" href="/report-status?refresh=1">↻ Refresh now</a>
  <div class="foot">
    A warehouse shows received only when a rep sent a real spreadsheet/CSV or a
    multi-variety data table this week — signature images and prose mentions do
    not count. Cheney's three FL facilities clear off Michael Ross's single
    report. Read-only via Microsoft Graph; times approximate US Eastern.
  </div>
</div></body></html>"""


__all__ = ["compute_report_status", "get_status", "render_html",
           "most_recent_friday", "WAREHOUSE_REPS"]
