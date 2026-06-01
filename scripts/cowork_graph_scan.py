#!/usr/bin/env python3
"""Cowork-direct mailbox scan via Microsoft Graph.

Replaces the Outlook-MCP path used by the `inventory-mailbox-scan-4h` Cowork
scheduled task. The Outlook MCP that ships with Claude Cowork does not surface
attachment bytes (its `read_resource` URI scheme has no `/attachments` path),
which made the original SKILL.md flow unworkable. This script bypasses the MCP
entirely: it talks to Microsoft Graph from PowerShell-launched Python, using
the same client-credentials flow the Render web service already uses.

Usage:
    python scripts\\cowork_graph_scan.py [--dry-run] [--lookback-hours 8]
                                         [--state <path>] [--verbose]

Required environment variables (or pass via flags / use `--graph-creds-file`):
    MS365_TENANT_ID
    MS365_CLIENT_ID
    MS365_CLIENT_SECRET
    INVENTORY_API_TOKEN          token for the Render service
    APP_URL                      e.g. https://bagel-inventory.onrender.com
    MAILBOXES                    comma-separated; default
                                 "JD@ms.hhbagels.com,info@ms.hhbagels.com"

Optional:
    SEEN_STATE_FILE              path to a plain-text file with one Graph
                                 message ID per line (created if missing,
                                 truncated to last 500 entries)

Exit codes:
    0   nothing-new OR ingest-events returned 200 OK
    1   transient or recoverable failure (caller may retry)
    2   misconfiguration (missing creds, bad APP_URL, etc.)

Outputs a single one-line summary on stdout suitable for the Cowork scheduled
task's `.log` line, plus optional verbose details on stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64decode
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

# Ensure the repo root is on sys.path so we can import `integrations`.
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from integrations.email_scanner import (  # noqa: E402
    _usfoods_po_to_events,
    _cheney_po_to_events,
    _chefs_warehouse_po_to_record,
    _inventory_worksheet_to_events,
    _usfoods_inventory_report_to_events,
)
from integrations.bagel_inventory_worksheet import (  # noqa: E402
    warehouse_for_sender as _worksheet_warehouse_for_sender,
)
from integrations.lineage_freight_parser import (  # noqa: E402
    parse_freight_pdf,
)


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

DEFAULT_MAILBOXES = "JD@ms.hhbagels.com,info@ms.hhbagels.com"

# Keys we treat as PO-bearing senders. Other senders are ignored even if
# the subject contains "Purchase Order".
USF_DOMAINS     = {"usfoods.com", "usfood.com"}
CHENEY_DOMAINS  = {"cheneybrothers.com", "cheney.com"}
CHEFS_DOMAINS   = {"chefswarehouse.com"}
# Lineage Freight TMS sends invoice notification emails from a BluJay
# Solutions noreply address. The subject always contains "Billable
# Invoice(s) from LINEAGE FREIGHT MANAGEMENT LLC".
LINEAGE_DOMAINS = {"tms.blujaysolutions.net", "blujaysolutions.net",
                   "tms.e2open.com", "e2open.com",
                   "lineagelogistics.com", "onelineage.com"}


# ---------------------------------------------------------------------------
# small utils
# ---------------------------------------------------------------------------

def _vlog(verbose: bool, *args, **kwargs) -> None:
    if verbose:
        print(*args, file=sys.stderr, **kwargs)


def _redact(text: str, secrets: Iterable[str]) -> str:
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


def _domain_of(addr: str) -> str:
    m = re.search(r"@([\w.\-]+)", addr or "")
    return m.group(1).lower() if m else ""


def _classify(sender: str, subject: str) -> str | None:
    """Return 'US Foods' | 'Cheney Brothers' | 'Chefs Warehouse' | None."""
    dom = _domain_of(sender)
    for d in USF_DOMAINS:
        if dom == d or dom.endswith("." + d):
            return "US Foods"
    for d in CHENEY_DOMAINS:
        if dom == d or dom.endswith("." + d):
            return "Cheney Brothers"
    for d in CHEFS_DOMAINS:
        if dom == d or dom.endswith("." + d):
            return "Chefs Warehouse"
    # Lineage Freight TMS — sender domain check first (the noreply
    # address from BluJay Solutions), then fall back to a subject
    # match in case Lineage migrates to a different relay later.
    for d in LINEAGE_DOMAINS:
        if dom == d or dom.endswith("." + d):
            return "Lineage Freight"
    subj_u = (subject or "").upper()
    if "LINEAGE FREIGHT" in subj_u and "BILLABLE INVOICE" in subj_u:
        return "Lineage Freight"
    # Known inventory-worksheet reps may email from any address; honor the
    # explicit rep map so their weekly worksheets qualify even if the domain
    # isn't one of the distributor domains above.
    rep_dist, _rep_wh = _worksheet_warehouse_for_sender(sender)
    if rep_dist:
        return rep_dist
    # Some PO confirmations get forwarded by internal staff. We do NOT
    # accept those automatically — the original PDF is the source of truth
    # and will arrive in the original distributor message too.
    return None


# ---------------------------------------------------------------------------
# Graph auth + HTTP
# ---------------------------------------------------------------------------

def _graph_token(tenant: str, client_id: str, client_secret: str,
                 verbose: bool = False) -> str:
    """Mint an app-only access token via client_credentials."""
    url = GRAPH_TOKEN_URL.format(tenant=tenant)
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": GRAPH_DEFAULT_SCOPE,
        "grant_type": "client_credentials",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    _vlog(verbose, f"Graph token: POST {url}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Graph token failed: {exc.code} {exc.reason}: {body_txt[:300]}") from exc
    tok = payload.get("access_token")
    if not tok:
        raise RuntimeError(f"Graph token response missing access_token: {payload}")
    return tok


def _graph_get(token: str, path: str, *, verbose: bool = False) -> dict:
    """GET on Graph. `path` may be a relative path or a full URL."""
    url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    _vlog(verbose, f"Graph GET {url}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Graph GET {url} failed: {exc.code} {exc.reason}: {body_txt[:400]}") from exc


def _graph_get_bytes(token: str, path: str, *, verbose: bool = False) -> bytes:
    url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    _vlog(verbose, f"Graph GET (bytes) {url}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Graph GET {url} failed: {exc.code} {exc.reason}: {body_txt[:400]}") from exc


# ---------------------------------------------------------------------------
# mailbox scan
# ---------------------------------------------------------------------------

def _list_recent_messages(token: str, mailbox: str, since_dt: datetime,
                          *, verbose: bool = False) -> list[dict]:
    """Return list of recent messages with attachments for one mailbox.

    Graph rejects (`hasAttachments eq true and receivedDateTime ge X`) +
    `$orderby receivedDateTime` as `InefficientFilter`. We work around that
    by filtering on receivedDateTime alone (Graph returns most-recent-first
    by default) and screening `hasAttachments` client-side. That keeps the
    result set bounded by the lookback window and avoids the Graph hint.
    """
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    qs = urllib.parse.urlencode({
        "$select": ("id,subject,from,toRecipients,receivedDateTime,"
                    "hasAttachments,internetMessageId"),
        "$filter": f"receivedDateTime ge {since_iso}",
        "$top": "100",
    })
    out: list[dict] = []
    next_url = (f"/users/{urllib.parse.quote(mailbox)}/messages?{qs}")
    pages = 0
    while next_url and pages < 5:
        pages += 1
        page = _graph_get(token, next_url, verbose=verbose)
        for m in page.get("value") or []:
            if m.get("hasAttachments"):
                out.append(m)
        nxt = page.get("@odata.nextLink")
        next_url = nxt if nxt else None
    return out


def _list_message_attachments(token: str, mailbox: str, message_id: str,
                              *, verbose: bool = False) -> list[dict]:
    # Note: Graph rejects `@odata.type` in $select. The OData type is returned
    # automatically on each item alongside the selected fields, so just request
    # the user fields and let Graph annotate the results itself.
    qs = urllib.parse.urlencode({
        "$select": "id,name,contentType,size",
    })
    path = (f"/users/{urllib.parse.quote(mailbox)}"
            f"/messages/{urllib.parse.quote(message_id)}/attachments?{qs}")
    page = _graph_get(token, path, verbose=verbose)
    return page.get("value") or []


def _fetch_attachment_bytes(token: str, mailbox: str, message_id: str,
                            attachment_id: str, *, verbose: bool = False) -> bytes:
    """Fetch one attachment's raw bytes.

    Two paths:
      1. `/$value` returns raw bytes for FileAttachment (works for typical
         PDFs). It can return 415 for ItemAttachment / ReferenceAttachment.
      2. Fallback: GET the JSON form and decode `contentBytes` (base64).
    """
    base = (f"/users/{urllib.parse.quote(mailbox)}"
            f"/messages/{urllib.parse.quote(message_id)}"
            f"/attachments/{urllib.parse.quote(attachment_id)}")
    try:
        return _graph_get_bytes(token, f"{base}/$value", verbose=verbose)
    except RuntimeError as exc:
        _vlog(verbose, f"  /$value failed, falling back to JSON: {exc}")
        meta = _graph_get(token, base, verbose=verbose)
        content_b64 = meta.get("contentBytes")
        if not content_b64:
            raise RuntimeError(
                f"attachment {attachment_id[:12]}… has no contentBytes "
                f"(odata.type={meta.get('@odata.type')!r})"
            ) from exc
        return b64decode(content_b64)


def _fetch_message_body(token: str, mailbox: str, message_id: str,
                        *, verbose: bool = False) -> tuple[str, str]:
    """Return (html, text) for a message body via Graph.

    Prefers ``uniqueBody`` (just the latest reply, no quoted thread) so a
    US Foods rep's inline inventory table is parsed without the older quoted
    reports beneath it; falls back to the full ``body``.
    """
    path = (f"/users/{urllib.parse.quote(mailbox)}"
            f"/messages/{urllib.parse.quote(message_id)}"
            f"?$select=body,uniqueBody")
    meta = _graph_get(token, path, verbose=verbose)
    for key in ("uniqueBody", "body"):
        b = meta.get(key) or {}
        content = b.get("content") or ""
        if not content:
            continue
        if (b.get("contentType") or "").lower() == "html":
            return content, ""
        return "", content
    return "", ""


# ---------------------------------------------------------------------------
# state (seen-IDs file)
# ---------------------------------------------------------------------------

def _read_seen_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return {line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()}
    except OSError:
        return set()


def _write_seen_state(path: Path, ids: list[str], *, max_lines: int = 500) -> None:
    """Append new ids and truncate to the last max_lines rows."""
    existing = []
    if path.exists():
        try:
            existing = [line.strip() for line in
                        path.read_text(encoding="utf-8").splitlines()
                        if line.strip()]
        except OSError:
            existing = []
    seen = set(existing)
    for x in ids:
        if x and x not in seen:
            existing.append(x)
            seen.add(x)
    trimmed = existing[-max_lines:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(trimmed) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def _post_ingest(app_url: str, token: str, payload: dict,
                 *, path: str = "/api/email/ingest-events",
                 verbose: bool = False) -> tuple[int, dict | str]:
    url = f"{app_url.rstrip('/')}{path}"
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Inventory-Token": token,
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    _vlog(verbose, f"POST {url}  ({len(body)} bytes)")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload_text = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(payload_text)
            except ValueError:
                return resp.status, payload_text
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", errors="replace")
        return exc.code, body_txt


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="Run end-to-end but POST with dry_run=true.")
    p.add_argument("--lookback-hours", type=int, default=24,
                   help="How far back to look for new messages (default 24).")
    p.add_argument("--lineage-lookback-hours", type=int, default=0,
                   help="Override --lookback-hours just for Lineage Freight "
                        "messages (their invoices land weeks after ship date, "
                        "and they are rarer than POs so we can afford a "
                        "deeper sweep). 0 = use --lookback-hours.")
    # Env-var override path. When LOOKBACK_HOURS_OVERRIDE is set, it WINS
    # over both the default and the --lookback-hours CLI flag. Useful for
    # one-off backfills (e.g. 90-day catch-up after adding a new mailbox)
    # without having to edit the cron's startCommand. Unset = use --flag.
    p.add_argument("--mailboxes", default=os.environ.get("MAILBOXES", DEFAULT_MAILBOXES),
                   help="Comma-separated list. Default: env MAILBOXES or "
                        "JD@ms.hhbagels.com,info@ms.hhbagels.com")
    p.add_argument("--state", default=os.environ.get(
        "SEEN_STATE_FILE",
        str(REPO / ".cowork_seen_ids.txt"),
    ), help="Seen-IDs state file path. Default ~repo/.cowork_seen_ids.txt or "
            "$SEEN_STATE_FILE.")
    p.add_argument("--app-url", default=os.environ.get("APP_URL", "").rstrip("/"),
                   help="Base URL of the Inventory Tracker web service.")
    p.add_argument("--api-token", default=os.environ.get("INVENTORY_API_TOKEN", ""),
                   help="X-Inventory-Token value. Default $INVENTORY_API_TOKEN.")
    p.add_argument("--graph-creds-file", default="",
                   help="Optional path to a Markdown/text file containing "
                        "MS365_TENANT_ID=, MS365_CLIENT_ID=, MS365_CLIENT_SECRET= "
                        "lines; values override unset env vars.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    secrets_to_redact: list[str] = []

    # ---- creds
    tenant = os.environ.get("MS365_TENANT_ID", "").strip()
    client_id = os.environ.get("MS365_CLIENT_ID", "").strip()
    client_secret = os.environ.get("MS365_CLIENT_SECRET", "").strip()
    if args.graph_creds_file:
        try:
            txt = Path(args.graph_creds_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: cannot read {args.graph_creds_file}: {exc}", file=sys.stderr)
            return 2
        for line in txt.splitlines():
            line = line.strip()
            if "=" not in line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            k = k.strip().lstrip("`")
            v = v.strip().rstrip("`").strip()
            if k == "MS365_TENANT_ID" and not tenant:
                tenant = v
            elif k == "MS365_CLIENT_ID" and not client_id:
                client_id = v
            elif k == "MS365_CLIENT_SECRET" and not client_secret:
                client_secret = v
    missing = [k for k, v in {
        "MS365_TENANT_ID": tenant,
        "MS365_CLIENT_ID": client_id,
        "MS365_CLIENT_SECRET": client_secret,
        "APP_URL": args.app_url,
        "INVENTORY_API_TOKEN": args.api_token,
    }.items() if not v]
    if missing:
        print(f"ERROR: missing creds: {', '.join(missing)}", file=sys.stderr)
        return 2
    secrets_to_redact.extend([client_secret, args.api_token])

    mailboxes = [m.strip() for m in args.mailboxes.split(",") if m.strip()]
    state_path = Path(args.state).expanduser()
    seen_ids = _read_seen_state(state_path)

    # Resolve effective lookback: LOOKBACK_HOURS_OVERRIDE env var beats the
    # CLI flag (the cron's startCommand hardcodes --lookback-hours 24, which
    # we want to be able to override at runtime for one-off backfills).
    lookback_override_raw = os.environ.get("LOOKBACK_HOURS_OVERRIDE", "").strip()
    lookback_hours = args.lookback_hours
    if lookback_override_raw:
        try:
            lookback_hours = int(lookback_override_raw)
            _vlog(args.verbose,
                  f"LOOKBACK_HOURS_OVERRIDE={lookback_override_raw} overrides "
                  f"--lookback-hours {args.lookback_hours}")
        except ValueError:
            _vlog(args.verbose,
                  f"LOOKBACK_HOURS_OVERRIDE={lookback_override_raw!r} is not "
                  f"an integer; keeping --lookback-hours {args.lookback_hours}")

    _vlog(args.verbose, f"seen-set size at start: {len(seen_ids)}")
    _vlog(args.verbose, f"mailboxes: {mailboxes}")
    _vlog(args.verbose, f"lookback: {lookback_hours}h")

    # ---- Graph token
    try:
        token = _graph_token(tenant, client_id, client_secret, verbose=args.verbose)
    except Exception as exc:
        msg = _redact(str(exc), secrets_to_redact)
        print(f"ERROR: Graph token failed: {msg}", file=sys.stderr)
        return 1

    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    # ---- discover qualifying messages
    qualifying: list[dict] = []   # [{mailbox, id, subject, sender, distributor}]
    mailbox_diag: list[str] = []  # one line per mailbox, always logged
    for mb in mailboxes:
        try:
            msgs = _list_recent_messages(token, mb, since, verbose=args.verbose)
        except Exception as exc:
            msg = _redact(str(exc), secrets_to_redact)
            # ALWAYS surface mailbox-list failures (not just under --verbose).
            # Silent failures here are why a misconfigured mailbox can sit
            # broken for weeks without anyone noticing.
            err_line = f"list_recent_messages({mb}) failed: {msg}"
            print(f"ERROR: {err_line}", file=sys.stderr)
            mailbox_diag.append(f"{mb}: ERROR ({msg[:120]})")
            continue
        # Count senders we recognise so we can tell "scanner found CW emails
        # but skipped them" from "scanner saw no relevant senders at all".
        by_dist: dict[str, int] = {}
        for m in msgs:
            sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""
            dist = _classify(sender, m.get("subject") or "")
            if dist:
                by_dist[dist] = by_dist.get(dist, 0) + 1
        diag = (f"{mb}: {len(msgs)} msgs with attachments"
                + (f"; matched: " + ", ".join(f"{d}={n}" for d, n in by_dist.items())
                   if by_dist else "; matched: 0 (no tracked senders)"))
        print(diag, file=sys.stderr)
        mailbox_diag.append(diag)
        for m in msgs:
            mid = m.get("id") or ""
            if mid in seen_ids:
                continue
            sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""
            subject = m.get("subject") or ""
            dist = _classify(sender, subject)
            if not dist:
                continue
            qualifying.append({
                "mailbox": mb,
                "id": mid,
                "subject": subject,
                "sender": sender,
                "distributor": dist,
            })

    if not qualifying:
        print(f"nothing new; seen-set size = {len(seen_ids)}")
        return 0

    # ---- fetch attachments + parse
    events_out: list[dict] = []     # USF + Cheney -> /api/email/ingest-events
    cw_pos_out: list[dict] = []     # Chefs Warehouse -> /api/chefs-warehouse/ingest-pos
    freight_out: list[dict] = []    # Lineage Freight  -> /api/freight/ingest
    error_strs: list[str] = []
    msgs_parsed = 0

    XLSX_CTYPE = ("application/vnd.openxmlformats-officedocument"
                  ".spreadsheetml.sheet")
    for q in qualifying:
        mb, mid, subject, dist, sender = (
            q["mailbox"], q["id"], q["subject"], q["distributor"], q["sender"],
        )
        events_before = len(events_out)
        try:
            atts = _list_message_attachments(token, mb, mid, verbose=args.verbose)
        except Exception as exc:
            msg = _redact(str(exc), secrets_to_redact)
            error_strs.append(f"{mid[:12]}.. [list-att]: {msg}")
            continue

        # Lineage Freight attachments are .zip files containing one or
        # more PDFs. Everything else (USF/Cheney/CW) is a direct PDF.
        wanted_zip = (dist == "Lineage Freight")
        any_pdf = False
        for a in atts:
            name = (a.get("name") or "").lower()
            ctype = (a.get("contentType") or "").lower()
            is_pdf = name.endswith(".pdf") or ctype == "application/pdf"
            is_zip = (wanted_zip and
                      (name.endswith(".zip") or ctype in ("application/zip",
                                                          "application/x-zip-compressed")))
            is_xlsx = name.endswith(".xlsx") or ctype == XLSX_CTYPE
            if not (is_pdf or is_zip or is_xlsx):
                continue
            any_pdf = True
            try:
                pdf_bytes = _fetch_attachment_bytes(
                    token, mb, mid, a.get("id") or "", verbose=args.verbose,
                )
            except Exception as exc:
                msg = _redact(str(exc), secrets_to_redact)
                error_strs.append(f"{mid[:12]}.. [fetch-att]: {msg}")
                continue
            try:
                if is_xlsx:
                    # US Foods inventory & usage report (.xlsx): Manassas
                    # "Product Usage" / La Mirada "SM Inventory". Try the report
                    # parser first; fall back to the older CS OH / WKLY USE
                    # worksheet parser when this isn't a report layout.
                    events, errors = _usfoods_inventory_report_to_events(
                        "", "", sender, mid, subject, xlsx=pdf_bytes)
                    if not events and not errors:
                        events, errors = _inventory_worksheet_to_events(
                            pdf_bytes, sender, mid, subject)
                    for e in events:
                        events_out.append(asdict(e))
                    for er in errors:
                        error_strs.append(f"{mid[:12]}.. [{dist}]: {er}")
                elif dist == "US Foods":
                    events, errors = _usfoods_po_to_events(
                        pdf_bytes, dist, mid, subject)
                    for e in events:
                        events_out.append(asdict(e))
                    for er in errors:
                        error_strs.append(f"{mid[:12]}.. [{dist}]: {er}")
                elif dist == "Cheney Brothers":
                    events, errors = _cheney_po_to_events(
                        pdf_bytes, dist, mid, subject)
                    for e in events:
                        events_out.append(asdict(e))
                    for er in errors:
                        error_strs.append(f"{mid[:12]}.. [{dist}]: {er}")
                elif dist == "Chefs Warehouse":
                    record, errors = _chefs_warehouse_po_to_record(
                        pdf_bytes, mid, subject)
                    if record is not None:
                        cw_pos_out.append(record)
                    for er in errors:
                        error_strs.append(f"{mid[:12]}.. [{dist}]: {er}")
                elif dist == "Lineage Freight":
                    # Unzip and parse each PDF inside.
                    import io as _io
                    import zipfile as _zipfile
                    from dataclasses import asdict as _asdict
                    pdfs: list[tuple[str, bytes]] = []
                    if is_zip:
                        try:
                            zf = _zipfile.ZipFile(_io.BytesIO(pdf_bytes))
                            for info in zf.infolist():
                                if info.filename.lower().endswith(".pdf"):
                                    pdfs.append((info.filename, zf.read(info.filename)))
                        except _zipfile.BadZipFile as exc:
                            error_strs.append(
                                f"{mid[:12]}.. [Lineage]: bad zip: {exc}")
                            pdfs = []
                    else:
                        # Some Lineage messages arrive with a single PDF
                        # directly attached. Accept that too.
                        pdfs = [(a.get("name") or "invoice.pdf", pdf_bytes)]
                    for fname, pb in pdfs:
                        try:
                            inv = parse_freight_pdf(
                                pb, pdf_filename=fname,
                                source_message_id=mid, source_subject=subject)
                        except Exception as exc:
                            error_strs.append(
                                f"{mid[:12]}.. [Lineage/{fname}]: parse failed: {exc}")
                            continue
                        if inv is None:
                            error_strs.append(
                                f"{mid[:12]}.. [Lineage/{fname}]: not a freight invoice")
                            continue
                        freight_out.append(_asdict(inv))
                else:
                    error_strs.append(
                        f"{mid[:12]}.. [{dist}]: no parser for distributor")
            except Exception as exc:
                msg = _redact(str(exc), secrets_to_redact)
                error_strs.append(f"{mid[:12]}.. [parse]: {msg}")
                continue
        # US Foods inventory & usage report pasted into the message body
        # (Zebulon): no PDF/xlsx attachment carries it. When the attachments
        # produced no events for a US Foods message, fetch the body and parse
        # the inline table.
        if dist == "US Foods" and len(events_out) == events_before:
            try:
                html_body, text_body = _fetch_message_body(
                    token, mb, mid, verbose=args.verbose)
            except Exception as exc:
                html_body, text_body = "", ""
                error_strs.append(
                    f"{mid[:12]}.. [body-fetch]: "
                    f"{_redact(str(exc), secrets_to_redact)}")
            if html_body or text_body:
                b_events, b_errs = _usfoods_inventory_report_to_events(
                    html_body, text_body, sender, mid, subject)
                for e in b_events:
                    events_out.append(asdict(e))
                for er in b_errs:
                    error_strs.append(f"{mid[:12]}.. [{dist}]: {er}")

        if any_pdf or len(events_out) > events_before:
            msgs_parsed += 1
        else:
            error_strs.append(
                f"{mid[:12]}.. [{dist}]: no PDF/xlsx/zip attachment found")

    # ---- POST events (USF + Cheney) to /api/email/ingest-events
    rep0: dict = {}
    rep_status = "ok"
    rep_updated = 0
    rep_errors = 0

    if events_out or not cw_pos_out:
        # Always POST events when we have any; also POST an empty batch
        # when there were no events AND no CW POs so the upstream "no
        # new mail" path stays unchanged.
        payload = {
            "dry_run": bool(args.dry_run),
            "source": "cowork-routine/graph-direct",
            "messages_seen": len(qualifying),
            "messages_parsed": msgs_parsed,
            "errors": error_strs,
            "events": events_out,
        }
        try:
            status, body = _post_ingest(args.app_url, args.api_token, payload,
                                        verbose=args.verbose)
        except Exception as exc:
            msg = _redact(str(exc), secrets_to_redact)
            print(f"ERROR: POST failed: {msg}", file=sys.stderr)
            return 1
        if status != 200 or not isinstance(body, dict):
            body_text = body if isinstance(body, str) else json.dumps(body)[:400]
            body_text = _redact(body_text, secrets_to_redact)
            print(f"ERROR: ingest-events HTTP {status}: {body_text}", file=sys.stderr)
            return 1
        reports = body.get("reports") or []
        rep0 = reports[0] if reports else {}
        rep_status = rep0.get("status") or "unknown"
        rep_updated = rep0.get("updated") or 0
        rep_errors  = len(rep0.get("error") and [rep0["error"]] or []) + len(error_strs)

    # ---- POST CW POs to /api/chefs-warehouse/ingest-pos
    cw_report: dict = {}
    if cw_pos_out:
        cw_payload = {
            "dry_run": bool(args.dry_run),
            "source": "cowork-routine/graph-direct",
            "cw_pos": cw_pos_out,
        }
        try:
            cw_status, cw_body = _post_ingest(
                args.app_url, args.api_token, cw_payload,
                path="/api/chefs-warehouse/ingest-pos",
                verbose=args.verbose,
            )
        except Exception as exc:
            msg = _redact(str(exc), secrets_to_redact)
            print(f"ERROR: CW POST failed: {msg}", file=sys.stderr)
            return 1
        if cw_status != 200 or not isinstance(cw_body, dict):
            body_text = (cw_body if isinstance(cw_body, str)
                         else json.dumps(cw_body)[:400])
            body_text = _redact(body_text, secrets_to_redact)
            print(f"ERROR: chefs-warehouse/ingest-pos HTTP {cw_status}: {body_text}",
                  file=sys.stderr)
            return 1
        cw_report = (cw_body or {}).get("report") or {}

    # ---- POST freight invoices (Lineage) to /api/freight/ingest
    freight_report: dict = {}
    if freight_out:
        fr_payload = {
            "dry_run": bool(args.dry_run),
            "source": "cowork-routine/graph-direct",
            "invoices": freight_out,
        }
        try:
            fr_status, fr_body = _post_ingest(
                args.app_url, args.api_token, fr_payload,
                path="/api/freight/ingest",
                verbose=args.verbose,
            )
        except Exception as exc:
            msg = _redact(str(exc), secrets_to_redact)
            print(f"ERROR: Freight POST failed: {msg}", file=sys.stderr)
            return 1
        if fr_status != 200 or not isinstance(fr_body, dict):
            body_text = (fr_body if isinstance(fr_body, str)
                         else json.dumps(fr_body)[:400])
            body_text = _redact(body_text, secrets_to_redact)
            print(f"ERROR: freight/ingest HTTP {fr_status}: {body_text}",
                  file=sys.stderr)
            return 1
        freight_report = (fr_body or {}).get("report") or {}

    # ---- update seen-IDs only on real success (skip on dry-run)
    if not args.dry_run and rep_status in ("ok", "not_configured"):
        try:
            _write_seen_state(state_path, [q["id"] for q in qualifying])
        except OSError as exc:
            _vlog(args.verbose, f"failed to update seen-state: {exc}")

    new_seen_size = len(_read_seen_state(state_path))
    # Show the full ingest breakdown so the summary is self-explanatory.
    # The previous (fetched / added / updated) line was misleading: when
    # every PO matched an existing record, the line read "8 fetched / 0
    # added / 0 updated" and looked like the apply path was dropping
    # everything on the floor — when it was actually counting them as
    # "unchanged" (right behaviour, just invisible).
    cw_summary = ""
    if cw_pos_out:
        unchanged = cw_report.get("unchanged", 0)
        skipped_cancel = cw_report.get("skipped_canceled", 0)
        skipped_inval = cw_report.get("skipped_invalid", 0)
        parts = [
            f"{cw_report.get('fetched', 0)} fetched",
            f"{cw_report.get('added', 0)} added",
            f"{cw_report.get('updated', 0)} updated",
            f"{unchanged} unchanged",
        ]
        if skipped_cancel:
            parts.append(f"{skipped_cancel} canceled-skipped")
        if skipped_inval:
            parts.append(f"{skipped_inval} invalid-skipped")
        cw_summary = "; CW: " + " / ".join(parts)
    freight_summary = ""
    if freight_out:
        fparts = [
            f"{freight_report.get('added', 0)} added",
            f"{freight_report.get('updated', 0)} updated",
            f"{freight_report.get('skipped', 0)} skipped",
            f"total now {freight_report.get('total_after', 0)}",
        ]
        freight_summary = "; FREIGHT: " + " / ".join(fparts)
    print(
        f"ingest-events {'DRY ' if args.dry_run else ''}OK: "
        f"{msgs_parsed} parsed, {len(events_out)} events, "
        f"{len(cw_pos_out)} cw_pos, {len(freight_out)} freight, "
        f"{rep_errors} errors; status={rep_status}; updated={rep_updated}; "
        f"seen-set now {new_seen_size}{cw_summary}{freight_summary}"
    )
    if args.verbose:
        _vlog(True, "report (full):")
        _vlog(True, json.dumps(rep0, indent=2))
        if cw_report:
            _vlog(True, "CW report:")
            _vlog(True, json.dumps(cw_report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(run())
