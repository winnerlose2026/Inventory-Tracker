"""Weekly inventory-report status — who has / hasn't sent this week's report.

Powers the cloud-independent status page (``/report-status``) so JD can see,
from any device, which distributor warehouses have sent their weekly bagel
inventory & usage report and which are still outstanding — without the Cowork
desktop app running.

Detection mirrors the chaser logic: the WAREHOUSE is the unit, not the person.
A warehouse counts as reported if ANY of its covering reps sent a genuine
inventory/usage report since the most recent Friday (the cadence is a Monday
report; Friday is an early-bird cutoff). Cheney's three FL facilities all clear
off Michael Ross's single combined report.

Read-only: uses the same MS365 client-credentials (Mail.Read) the rest of the
app already uses; it sends nothing. All times handled in UTC; the page renders
an approximate US-Eastern (EDT) label for readability.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import json as _json
import os as _os
import time as _time
import urllib.error as _uerr
import urllib.parse as _url
import urllib.request as _ureq

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Warehouse -> covering reps. Warehouse labels match seed_bagels.py exactly.
# Multiple reps can cover one warehouse; any one of them sending clears it.
# Cheney: Michael Ross sends ONE report covering all three FL facilities, so
# his address is listed against each — one report clears all three.
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
    "Riviera Beach, FL": [
        {"name": "Michael Ross", "email": "mross@cheneybrothers.com"},
    ],
    "Ocala, FL": [
        {"name": "Michael Ross", "email": "mross@cheneybrothers.com"},
    ],
    "Punta Gorda, FL": [
        {"name": "Michael Ross", "email": "mross@cheneybrothers.com"},
    ],
}

DISTRIBUTOR_OF = {
    "Manassas, VA": "US Foods", "Zebulon, NC": "US Foods", "La Mirada, CA": "US Foods",
    "Chicago, IL": "US Foods", "Alcoa, TN": "US Foods",
    "Riviera Beach, FL": "Cheney Brothers", "Ocala, FL": "Cheney Brothers",
    "Punta Gorda, FL": "Cheney Brothers",
}

_REPORT_KEYWORDS = ("inventory", "usage", "report", "on hand", "on-hand", "bagel")
_AUTO_REPLY_MARKERS = ("automatic reply", "out of office", "out-of-office",
                       "undeliverable", "delivery has failed", "delivery failure")

_CACHE = {"at": 0.0, "data": None}


def most_recent_friday(now: _dt.datetime) -> _dt.datetime:
    """Midnight (UTC) of the most recent Friday on or before ``now``."""
    days_since_fri = (now.weekday() - 4) % 7  # Mon=0 .. Fri=4 .. Sun=6
    fri = now - _dt.timedelta(days=days_since_fri)
    return fri.replace(hour=0, minute=0, second=0, microsecond=0)


def _email_to_name() -> "dict[str, str]":
    out = {}
    for reps in WAREHOUSE_REPS.values():
        for rep in reps:
            out[rep["email"].lower()] = rep["name"]
    return out


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


def _iter_messages(token: str, mailbox: str, since_iso: str, max_pages: int = 12):
    q = {
        "$select": "id,subject,from,receivedDateTime,hasAttachments,bodyPreview",
        "$top": "100",
        "$filter": f"receivedDateTime ge {since_iso}",
        "$orderby": "receivedDateTime desc",
    }
    url = f"{GRAPH_BASE}/users/{_url.quote(mailbox)}/messages?{_url.urlencode(q)}"
    pages = 0
    while url and pages < max_pages:
        data = _graph_get(token, url)
        for msg in data.get("value", []):
            yield msg
        url = data.get("@odata.nextLink")
        pages += 1


def _is_report(msg: dict) -> bool:
    subj = (msg.get("subject") or "").lower()
    if any(mk in subj for mk in _AUTO_REPLY_MARKERS):
        return False
    if msg.get("hasAttachments"):
        return True
    body = (msg.get("bodyPreview") or "").lower()
    return any(k in subj or k in body for k in _REPORT_KEYWORDS)


def _sender(msg: dict) -> str:
    return (((msg.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()


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
        "warehouses": [],
        "received": 0,
        "missing": 0,
        "scanned": 0,
        "error": None,
    }
    if not result["configured"]:
        result["error"] = ("MS365 not configured — set MS365_TENANT_ID/"
                           "MS365_CLIENT_ID/MS365_CLIENT_SECRET and "
                           "MAILBOXES (or MS365_USER).")
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
    for mailbox in mailboxes:
        try:
            for msg in _iter_messages(token, mailbox, since_iso):
                scanned += 1
                addr = _sender(msg)
                whs = rep_wh.get(addr)
                if not whs or not _is_report(msg):
                    continue
                rd = msg.get("receivedDateTime") or ""
                for wh in whs:
                    cur = best.get(wh)
                    if cur is None or rd > cur["received_at"]:
                        best[wh] = {
                            "received_at": rd,
                            "rep_name": names.get(addr, addr),
                            "rep_email": addr,
                            "mailbox": mailbox,
                            "subject": msg.get("subject") or "",
                            "has_attachment": bool(msg.get("hasAttachments")),
                        }
        except _uerr.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:  # noqa: BLE001
                pass
            result["error"] = (f"Graph query failed for {mailbox}: "
                               f"HTTP {exc.code} {detail}")
        except Exception as exc:  # noqa: BLE001
            result["error"] = (f"Graph query failed for {mailbox}: "
                               f"{type(exc).__name__}: {exc}")

    result["scanned"] = scanned
    for wh, reps in WAREHOUSE_REPS.items():
        detail = best.get(wh)
        result["warehouses"].append({
            "warehouse": wh,
            "distributor": DISTRIBUTOR_OF.get(wh, ""),
            "reps": reps,
            "status": "received" if detail else "missing",
            "detail": detail,
        })
        if detail:
            result["received"] += 1
        else:
            result["missing"] += 1
    return result


def get_status(max_age_sec: int = 1800, force: bool = False) -> dict:
    now = _time.time()
    cached = _CACHE.get("data")
    if (not force and cached is not None
            and (now - _CACHE.get("at", 0.0)) < max_age_sec):
        out = dict(cached)
        out["cached"] = True
        return out
    try:
        data = compute_report_status()
    except Exception as exc:  # noqa: BLE001
        data = {"generated_at": "", "configured": False, "warehouses": [],
                "received": 0, "missing": 0, "scanned": 0,
                "error": f"{type(exc).__name__}: {exc}"}
    if not data.get("error"):
        _CACHE.update(at=now, data=data)
    data = dict(data)
    data["cached"] = False
    return data


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
            who = esc(d.get("rep_name") or "")
            when = _fmt_et(d.get("received_at") or "")
            fmt = "spreadsheet" if d.get("has_attachment") else "in email body"
            note = f'from {who} · {esc(when)} · {fmt}'
        else:
            badge = '<span class="no">❌ missing</span>'
            chasing = ", ".join(esc(r["name"]) for r in wh.get("reps", [])
                                if not r["name"].startswith("USF "))
            note = f'<span class="muted">chasing: {chasing}</span>'
        rows.append(
            f'<tr><td class="wh">{esc(wh.get("warehouse") or "")}</td>'
            f'<td class="st">{badge}</td><td class="dt">{note}</td></tr>'
        )
    rows_html = "\n".join(rows)

    banner = ""
    if err:
        banner = f'<div class="err">⚠️ {esc(err)}</div>'
    elif total:
        tone = "allin" if missing == 0 else "pending"
        msg = ("All warehouses have reported this week 🎉" if missing == 0
               else f"{missing} of {total} warehouses still missing this week's report")
        banner = f'<div class="summary {tone}">{esc(msg)}</div>'

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
  tr.grp td {{ background: #f0f2f5; font-weight: 700; font-size: 12px;
              text-transform: uppercase; letter-spacing: .04em; color: #4b5563; border-top: none; }}
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
    A warehouse is marked received if any of its reps sent this week's bagel
    inventory &amp; usage report. Cheney's three FL facilities clear off Michael
    Ross's single report. Detected read-only from the JD@ / info@ mailboxes via
    Microsoft Graph; times approximate US Eastern. Auto-refreshes every 15 min.
  </div>
</div></body></html>"""


__all__ = ["compute_report_status", "get_status", "render_html",
           "most_recent_friday", "WAREHOUSE_REPS"]
