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
)


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

DEFAULT_MAILBOXES = "JD@ms.hhbagels.com,info@ms.hhbagels.com"

# Keys we treat as PO-bearing senders. Other senders are ignored even if
# the subject contains "Purchase Order".
USF_DOMAINS = {"usfoods.com", "usfood.com"}
CHENEY_DOMAINS = {"cheneybrothers.com", "cheney.com"}
# H&H's own domain — used to recognize internal Daily Production sheet
# emails. The production team sends them between hhbagels.com addresses
# (typically gabo@ -> isaiah@, with JD@ on cc/bcc), so neither
# sender nor recipient looks like a distributor.
HH_DOMAINS = {"hhbagels.com", "ms.hhbagels.com"}
PRODUCTION_SUBJECT_RE = re.compile(r"\bDaily\s+[Pp]roduction\b")


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
    """Return 'US Foods' | 'Cheney Brothers' | None."""
    dom = _domain_of(sender)
    for d in USF_DOMAINS:
        if dom == d or dom.endswith("." + d):
            return "US Foods"
    for d in CHENEY_DOMAINS:
        if dom == d or dom.endswith("." + d):
            return "Cheney Brothers"
    # Some PO confirmations get forwarded by internal staff. We do NOT
    # accept those automatically — the original PDF is the source of truth
    # and will arrive in the original distributor message too.
    return None


def _is_production_email(sender: str, subject: str) -> bool:
    """Return True for H&H internal "Daily Production Sheet" emails.

    These don't go through the PO ingest path — they describe what was
    BAKED for a PO, not the PO itself. They're sent within H&H from the
    production team. Sender + subject pattern; safer than relying on
    either alone.
    """
    dom = _domain_of(sender)
    if dom not in HH_DOMAINS and not any(dom.endswith("." + d) for d in HH_DOMAINS):
        return False
    return bool(PRODUCTION_SUBJECT_RE.search(subject or ""))


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
    """Return attachment-bearing messages received since `since_dt`.

    `hasAttachments eq true and receivedDateTime ge X` is valid without
    `$orderby` — the combination errors as `InefficientFilter` when paired
    with an explicit sort. Without orderby Graph still returns every match;
    paging just isn't strictly date-ordered. Filtering server-side keeps a
    high-volume mailbox from burning the page budget on non-PO mail.
    """
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    qs = urllib.parse.urlencode({
        "$select": ("id,subject,from,toRecipients,receivedDateTime,"
                    "hasAttachments,internetMessageId"),
        "$filter": f"hasAttachments eq true and receivedDateTime ge {since_iso}",
        "$top": "100",
    })
    out: list[dict] = []
    next_url = (f"/users/{urllib.parse.quote(mailbox)}/messages?{qs}")
    pages = 0
    while next_url and pages < 20:
        pages += 1
        page = _graph_get(token, next_url, verbose=verbose)
        out.extend(page.get("value") or [])
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
                 *, verbose: bool = False) -> tuple[int, dict | str]:
    url = f"{app_url.rstrip('/')}/api/email/ingest-events"
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
    _vlog(args.verbose, f"seen-set size at start: {len(seen_ids)}")
    _vlog(args.verbose, f"mailboxes: {mailboxes}")
    _vlog(args.verbose, f"lookback: {args.lookback_hours}h")

    # ---- Graph token
    try:
        token = _graph_token(tenant, client_id, client_secret, verbose=args.verbose)
    except Exception as exc:
        msg = _redact(str(exc), secrets_to_redact)
        print(f"ERROR: Graph token failed: {msg}", file=sys.stderr)
        return 1

    since = datetime.now(timezone.utc) - timedelta(hours=args.lookback_hours)

    # ---- discover qualifying messages
    #
    # A message qualifies only if (a) the sender is Cheney/USF and (b) at least
    # one attachment is a PDF. Non-PDF attachments (calendar invites, images,
    # docx forwards from buyer threads) are skipped silently — we only parse
    # PO PDFs here.
    error_strs: list[str] = []
    qualifying: list[dict] = []   # PO emails {mailbox, id, subject, sender, distributor, pdf_atts}
    production_qualifying: list[dict] = []  # Daily Production emails
    for mb in mailboxes:
        try:
            msgs = _list_recent_messages(token, mb, since, verbose=args.verbose)
        except Exception as exc:
            msg = _redact(str(exc), secrets_to_redact)
            _vlog(args.verbose, f"  list_recent_messages({mb}) failed: {msg}")
            continue
        _vlog(args.verbose, f"  {mb}: {len(msgs)} recent messages with attachments")
        for m in msgs:
            mid = m.get("id") or ""
            if mid in seen_ids:
                continue
            sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""
            subject = m.get("subject") or ""
            received = m.get("receivedDateTime") or ""
            dist = _classify(sender, subject)
            is_prod = _is_production_email(sender, subject)
            if not dist and not is_prod:
                continue
            try:
                atts = _list_message_attachments(token, mb, mid, verbose=args.verbose)
            except Exception as exc:
                msg = _redact(str(exc), secrets_to_redact)
                error_strs.append(f"{mid[:12]}.. [list-att]: {msg}")
                continue
            pdf_atts = [
                a for a in atts
                if (a.get("name") or "").lower().endswith(".pdf")
                or (a.get("contentType") or "").lower() == "application/pdf"
            ]
            if not pdf_atts:
                continue
            entry = {
                "mailbox": mb,
                "id": mid,
                "subject": subject,
                "sender": sender,
                "received_at": received,
                "pdf_atts": pdf_atts,
            }
            if dist:
                entry["distributor"] = dist
                qualifying.append(entry)
            if is_prod:
                # Production emails are processed via a separate path;
                # collect them in a sibling list. Both paths share the
                # same seen-IDs set so we never re-process the same msg.
                production_qualifying.append(entry)

    if not qualifying and not production_qualifying:
        print(f"nothing new; seen-set size = {len(seen_ids)}")
        return 0

    # ---- fetch attachments + parse
    events_out: list[dict] = []
    msgs_parsed = 0

    for q in qualifying:
        mb, mid, subject, dist = q["mailbox"], q["id"], q["subject"], q["distributor"]
        for a in q["pdf_atts"]:
            try:
                pdf_bytes = _fetch_attachment_bytes(
                    token, mb, mid, a.get("id") or "", verbose=args.verbose,
                )
            except Exception as exc:
                msg = _redact(str(exc), secrets_to_redact)
                error_strs.append(f"{mid[:12]}.. [fetch-att]: {msg}")
                continue
            fn = _usfoods_po_to_events if dist == "US Foods" else _cheney_po_to_events
            try:
                events, errors = fn(pdf_bytes, dist, mid, subject)
            except Exception as exc:
                msg = _redact(str(exc), secrets_to_redact)
                error_strs.append(f"{mid[:12]}.. [parse]: {msg}")
                continue
            for e in events:
                events_out.append(asdict(e))
            for er in errors:
                error_strs.append(f"{mid[:12]}.. [{dist}]: {er}")
        msgs_parsed += 1

    payload = {
        "dry_run": bool(args.dry_run),
        "source": "cowork-routine/graph-direct",
        "messages_seen": len(qualifying),
        "messages_parsed": msgs_parsed,
        "errors": error_strs,
        "events": events_out,
    }

    # ---- POST to ingest-events
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
    rep_errors = len(rep0.get("error") and [rep0["error"]] or []) + len(error_strs)

    # ---- production POSTs (independent of PO ingest)
    prod_posted = 0
    prod_errors: list[str] = []
    for q in production_qualifying:
        mb, mid, subject, sender = q["mailbox"], q["id"], q["subject"], q["sender"]
        received_at = q.get("received_at", "")
        for a in q["pdf_atts"]:
            try:
                pdf_bytes = _fetch_attachment_bytes(
                    token, mb, mid, a.get("id") or "", verbose=args.verbose,
                )
            except Exception as exc:
                msg = _redact(str(exc), secrets_to_redact)
                prod_errors.append(f"{mid[:12]}.. [prod-fetch]: {msg}")
                continue
            import base64
            prod_payload = {
                "pdf_b64":     base64.b64encode(pdf_bytes).decode(),
                "subject":     subject,
                "sender":      sender,
                "message_id":  mid,
                "received_at": received_at,
            }
            try:
                url = f"{args.app_url.rstrip('/')}/api/production/ingest"
                req = urllib.request.Request(
                    url, data=json.dumps(prod_payload).encode(),
                    headers={"Content-Type": "application/json",
                             "X-Inventory-Token": args.api_token},
                    method="POST")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    d = json.loads(resp.read().decode())
                if d.get("status") == "ingested":
                    prod_posted += 1
                elif d.get("status") == "parse_error":
                    prod_errors.append(f"{mid[:12]}.. [prod-parse]: {d.get('error','')}")
            except Exception as exc:
                msg = _redact(str(exc), secrets_to_redact)
                prod_errors.append(f"{mid[:12]}.. [prod-post]: {msg}")

    # ---- update seen-IDs only on real success (skip on dry-run)
    if not args.dry_run and status == 200 and rep_status in ("ok", "not_configured"):
        try:
            _write_seen_state(state_path,
                               [q["id"] for q in qualifying]
                               + [q["id"] for q in production_qualifying])
        except OSError as exc:
            _vlog(args.verbose, f"failed to update seen-state: {exc}")

    new_seen_size = len(_read_seen_state(state_path))
    print(
        f"ingest-events {'DRY ' if args.dry_run else ''}OK: "
        f"{msgs_parsed} parsed, {len(events_out)} events, "
        f"{rep_errors} errors; status={rep_status}; updated={rep_updated}; "
        f"seen-set now {new_seen_size}; "
        f"prod-ingested {prod_posted}, prod-errors {len(prod_errors)}"
    )
    if args.verbose:
        _vlog(True, "report (full):")
        _vlog(True, json.dumps(rep0, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(run())
