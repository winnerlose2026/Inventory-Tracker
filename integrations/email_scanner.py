"""Email inbox scanner for inventory and usage information.

Pulls messages from the user's mailbox and extracts three kinds of signals:

  - **on_hand**   — order confirmations / cycle counts that report current
                    stock at a distributor warehouse. Replaces the local
                    quantity (same semantics as the CSV sync).
  - **restock**   — shipment notifications / invoices that announce units
                    delivered. Written to the usage log as a restock event.
  - **usage**     — daily / weekly consumption reports. Written to the usage
                    log as a consumption event.

Three mailbox sources are supported, tried in this order:

  1. **Microsoft 365 (Graph API)** — PRIMARY. OAuth 2.0 client-credentials
     flow against Azure AD; reads MIME directly from Microsoft Graph so the
     existing `parse_message` parser works unchanged.
  2. **Generic IMAP**              — for non-Microsoft mailboxes (Gmail, etc.)
     with an app password.
  3. **Local `.eml` dump folder**  — zero-config fallback for testing;
     drop sample messages in `integrations/email_dumps/`.

Input formats understood (in priority order):

  1. **US Foods PO PDF attachment** — when a message from a usfoods.com
     sender carries a PDF attachment (typical filename
     ``US Foods PO Request - <po#> - Date <mmyyyy>.PDF``), the PDF is
     parsed via ``usfoods_po_parser.parse_po_pdf`` and each line item is
     emitted as a ``restock`` event keyed to the PO's ship-to warehouse.

  2. **CSV attachment** on the message — parsed via the shared csv_loader.
     The attachment filename hints at the event type:
        *on_hand*.csv, *inventory*.csv      -> on_hand
        *invoice*.csv, *restock*.csv, *po*.csv -> restock
        *usage*.csv, *consumption*.csv      -> usage
     Default if no hint: on_hand.

  3. **Structured body text** — a tag line `# event: on_hand|restock|usage`
     followed by lines like `Plain @ Ocala, FL: 480 each` or
     `Plain Bagel 4oz [CB - Ocala]: 72`.

Distributor is inferred from the sender's domain (cheneybrothers.com ->
"Cheney Brothers", usfoods.com -> "US Foods"). An explicit
`# distributor: <name>` line in the body overrides the inference.

Configuration
-------------

**Microsoft 365 (preferred):**
  MS365_TENANT_ID       Azure AD tenant ID (GUID or verified domain)
  MS365_CLIENT_ID       app registration (client) ID
  MS365_CLIENT_SECRET   app registration client secret
  MS365_USER            mailbox to scan (UPN / email) — requires
                        Mail.Read application permission granted admin consent
  MS365_FOLDER          default "Inbox"
  MS365_FILTER          optional Graph $filter, e.g.
                        "isRead eq false and receivedDateTime ge 2025-01-01T00:00:00Z"
  MS365_MARK_READ       "1" to mark processed messages as read

**Generic IMAP fallback:**
  EMAIL_IMAP_HOST, EMAIL_IMAP_PORT (default 993), EMAIL_IMAP_USER,
  EMAIL_IMAP_PASSWORD, EMAIL_IMAP_FOLDER (default INBOX),
  EMAIL_IMAP_SEARCH (default 'UNSEEN SINCE <7-days-ago>'),
  EMAIL_IMAP_MARK_READ ("1" to mark read)
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import re
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Literal, Optional

from .base import NotConfiguredError, SyncItem
from .csv_loader import read_csv
from .usfoods_po_parser import (
    UsFoodsPO,
    UsFoodsPOLine,
    parse_po_pdf as _usfoods_parse_po_pdf,
)
from .cheney_po_parser import (
    CheneyPO,
    CheneyPOLine,
    parse_po_pdf as _cheney_parse_po_pdf,
)


EventType = Literal["on_hand", "restock", "usage"]

# Sender-domain -> our canonical distributor name.
DOMAIN_TO_DISTRIBUTOR = {
    "cheneybrothers.com": "Cheney Brothers",
    "cheney.com": "Cheney Brothers",
    "usfoods.com": "US Foods",
    "usfood.com": "US Foods",
}

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_TOKEN_URL_FMT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"


@dataclass
class EmailEvent:
    """One actionable signal pulled out of a message."""
    event_type: EventType
    item: SyncItem
    source_message_id: str = ""
    source_subject: str = ""
    # Populated only for events sourced from a distributor PO. The apply
    # path uses these to detect revisions: a later revision for the same
    # po_number fully supersedes earlier events (not line-by-line dedup).
    po_number: str = ""
    po_revision: str = ""


@dataclass
class ScanResult:
    source: str = ""              # "ms365" | "imap" | "dumps"
    messages_seen: int = 0
    messages_parsed: int = 0
    events: list[EmailEvent] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_BODY_ITEM_RE = re.compile(
    r"""^\s*
        (?:(?P<variety>[A-Za-z][A-Za-z \-]+?)\s+Bagel\s+4oz\s*
              (?:\[[^\]]+\])?                               # optional [CB - Ocala]
           |(?P<variety2>[A-Za-z][A-Za-z \-]+?)\s*@\s*(?P<warehouse>[^:]+?))
        \s*:\s*(?P<qty>\d+(?:\.\d+)?)
        (?:\s*(?P<unit>[A-Za-z]+))?
        \s*$""",
    re.VERBOSE,
)

_TAG_RE = re.compile(r"^\s*#\s*(event|distributor|warehouse)\s*:\s*(.+?)\s*$",
                     re.IGNORECASE | re.MULTILINE)

_FILENAME_TO_EVENT = [
    ("usage",       "usage"),
    ("consumption", "usage"),
    ("invoice",     "restock"),
    ("po",          "restock"),
    ("shipment",    "restock"),
    ("restock",     "restock"),
    ("on_hand",     "on_hand"),
    ("onhand",      "on_hand"),
    ("inventory",   "on_hand"),
    ("cycle",       "on_hand"),
]


def _infer_event_type_from_filename(name: str) -> EventType:
    low = name.lower()
    for needle, evt in _FILENAME_TO_EVENT:
        if needle in low:
            return evt  # type: ignore[return-value]
    return "on_hand"


def _distributor_from_sender(from_header: str) -> Optional[str]:
    m = re.search(r"@([\w.\-]+)", from_header or "")
    if not m:
        return None
    domain = m.group(1).lower()
    for known, name in DOMAIN_TO_DISTRIBUTOR.items():
        if domain == known or domain.endswith("." + known):
            return name
    return None


def _text_body(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        for part in msg.walk():
            if part.get_content_type().startswith("text/"):
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8",
                                      errors="replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")


def _attachments(msg: Message):
    for part in msg.walk() if msg.is_multipart() else [msg]:
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" not in disp and not part.get_filename():
            continue
        fname = part.get_filename() or ""
        payload = part.get_payload(decode=True) or b""
        yield fname, payload


def _parse_body_items(body, distributor, default_event):
    """Parse a structured body. Returns (event_type, warehouse_override, items)."""
    tags = {m.group(1).lower(): m.group(2).strip() for m in _TAG_RE.finditer(body)}
    event_type = tags.get("event", default_event).lower()
    if event_type not in ("on_hand", "restock", "usage"):
        event_type = default_event
    warehouse_override = tags.get("warehouse", "")
    items = []
    for line in body.splitlines():
        if line.strip().startswith("#"):
            continue
        m = _BODY_ITEM_RE.match(line)
        if not m:
            continue
        variety = (m.group("variety") or m.group("variety2") or "").strip()
        warehouse = (m.group("warehouse") or warehouse_override or "").strip()
        qty = float(m.group("qty"))
        unit = (m.group("unit") or "each").strip() or "each"
        items.append(SyncItem(
            quantity=qty,
            distributor=distributor,
            variety=variety or None,
            warehouse=warehouse or None,
            unit=unit,
        ))
    return event_type, warehouse_override, items


def _usfoods_po_to_events(pdf_bytes, distributor, msg_id, subject):
    """Parse a US Foods PO PDF and convert each line to a restock EmailEvent.

    Returns (events, errors). Errors are strings suitable for
    ``ScanResult.errors`` — e.g., unmapped USF item numbers or an unknown
    ship-to DC. One event is emitted per ``UsFoodsPOLine`` with an
    ``event_type`` of ``"restock"``.
    """
    events = []
    errors = []
    try:
        po = _usfoods_parse_po_pdf(pdf_bytes)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"usfoods PO PDF parse failed ({subject!r}): {exc}")
        return events, errors

    if po.unmapped_items:
        errors.append(
            f"usfoods PO {po.po_number or '?'}: unmapped USF item #s "
            f"{sorted(set(po.unmapped_items))} — add them to "
            "usfoods_po_parser.USF_ITEM_TO_VARIETY"
        )
    if po.ship_to_city and not po.warehouse:
        errors.append(
            f"usfoods PO {po.po_number or '?'}: unknown ship-to DC "
            f"{po.ship_to_city!r} — add it to "
            "usfoods_po_parser.USF_DC_CITY_TO_WAREHOUSE"
        )

    for line in po.lines:
        if not line.variety or not po.warehouse:
            continue
        events.append(EmailEvent(
            event_type="restock",
            item=SyncItem(
                quantity=line.quantity,
                distributor=distributor,
                variety=line.variety,
                warehouse=po.warehouse,
                unit=(line.unit or "cases").lower(),
                case_cost=line.net_cost,
                case_size=line.case_size,
                distributor_sku=line.usf_item_no,
            ),
            source_message_id=msg_id,
            source_subject=subject,
            po_number=po.po_number or "",
            po_revision=po.po_revision or "",
        ))

    return events, errors


def _cheney_po_to_events(pdf_bytes, distributor, msg_id, subject):
    """Parse a Cheney Brothers PO PDF and convert each line to a restock
    EmailEvent.

    Returns (events, errors). One event is emitted per ``CheneyPOLine``
    with an ``event_type`` of ``"restock"``. The PO number is preserved
    exactly as printed on the PDF (leading zeros retained). Cheney POs
    don't expose a revision number, so ``po_revision`` is emitted as ``""``.
    Cheney's ``Mfg#`` column is the same H&H internal SKU code that US
    Foods uses, so variety resolution shares ``hh_mfg_codes``.
    """
    events = []
    errors = []
    try:
        po = _cheney_parse_po_pdf(pdf_bytes)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"cheney PO PDF parse failed ({subject!r}): {exc}")
        return events, errors

    if po.unmapped_items:
        errors.append(
            f"cheney PO {po.po_number or '?'}: unmapped Mfg# codes "
            f"{sorted(set(po.unmapped_items))} — add them to "
            "hh_mfg_codes.HH_MFG_CODE_TO_VARIETY"
        )
    if po.ship_to_city and not po.warehouse:
        errors.append(
            f"cheney PO {po.po_number or '?'}: unknown ship-to DC "
            f"{po.ship_to_city!r} — add it to "
            "cheney_po_parser.CHENEY_DC_CITY_TO_WAREHOUSE"
        )

    for line in po.lines:
        if not line.variety or not po.warehouse:
            continue
        events.append(EmailEvent(
            event_type="restock",
            item=SyncItem(
                quantity=line.quantity,
                distributor=distributor,
                variety=line.variety,
                warehouse=po.warehouse,
                unit=(line.quantity_um or "CS").lower(),
                case_cost=line.net_cost,
                case_size=line.case_size,
                distributor_sku=line.mfg_code,
            ),
            source_message_id=msg_id,
            source_subject=subject,
            po_number=po.po_number or "",
            po_revision="",
        ))

    return events, errors


def parse_message_with_errors(msg):
    """Extract events from an email message and surface non-fatal issues.

    Attachment precedence:
      1. Distributor PO PDF attachment -> per-line ``restock`` events.
         Currently routed: US Foods (usfoods.com), Cheney Brothers
         (cheneybrothers.com).
      2. Any .csv attachment -> event type inferred from filename.
      3. Structured text body (tag lines) -> fallback if no attachments
         produced events.

    Returns (events, errors).
    """
    subject = str(msg.get("Subject", ""))
    msg_id = str(msg.get("Message-ID", ""))
    from_hdr = str(msg.get("From", ""))

    body = _text_body(msg)
    body_tags = {m.group(1).lower(): m.group(2).strip()
                 for m in _TAG_RE.finditer(body or "")}
    distributor = (body_tags.get("distributor")
                   or _distributor_from_sender(from_hdr)
                   or "Unassigned")

    events = []
    errors = []

    # 1) Distributor PO PDF attachments (real POs arrive as PDFs, not CSVs).
    for fname, payload in _attachments(msg):
        if not fname.lower().endswith(".pdf"):
            continue
        if distributor == "US Foods":
            d_events, d_errs = _usfoods_po_to_events(
                payload, distributor, msg_id, subject,
            )
        elif distributor == "Cheney Brothers":
            d_events, d_errs = _cheney_po_to_events(
                payload, distributor, msg_id, subject,
            )
        else:
            continue
        events.extend(d_events)
        errors.extend(d_errs)

    # 2) CSV attachments
    for fname, payload in _attachments(msg):
        if not fname.lower().endswith(".csv"):
            continue
        event_type = _infer_event_type_from_filename(fname)
        tmp = Path("/tmp") / f"_email_{abs(hash(msg_id + fname))}.csv"
        tmp.write_bytes(payload)
        try:
            for sync_item in read_csv(tmp, distributor=distributor):
                events.append(EmailEvent(
                    event_type=event_type,
                    item=sync_item,
                    source_message_id=msg_id,
                    source_subject=subject,
                ))
        finally:
            tmp.unlink(missing_ok=True)

    # 3) Structured body (only if no attachment produced events)
    if body and not events:
        event_type, _, items = _parse_body_items(body, distributor, "on_hand")
        for it in items:
            events.append(EmailEvent(
                event_type=event_type,
                item=it,
                source_message_id=msg_id,
                source_subject=subject,
            ))

    return events, errors


def parse_message(msg):
    """Thin wrapper over parse_message_with_errors that discards diagnostics."""
    events, _ = parse_message_with_errors(msg)
    return events


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class EmailInboxClient:
    """Scans a mailbox (MS 365 -> IMAP -> local dumps) for inventory events."""

    name = "Email Inbox"

    def _has_ms365_credentials(self):
        return bool(
            os.environ.get("MS365_TENANT_ID")
            and os.environ.get("MS365_CLIENT_ID")
            and os.environ.get("MS365_CLIENT_SECRET")
            and os.environ.get("MS365_USER")
        )

    def _has_imap_credentials(self):
        return bool(
            os.environ.get("EMAIL_IMAP_HOST")
            and os.environ.get("EMAIL_IMAP_USER")
            and os.environ.get("EMAIL_IMAP_PASSWORD")
        )

    def dumps_path(self):
        return Path(__file__).parent / "email_dumps"

    def source(self):
        if self._has_ms365_credentials():
            return "ms365"
        if self._has_imap_credentials():
            return "imap"
        if self.dumps_path().exists() and any(self.dumps_path().glob("*.eml")):
            return "dumps"
        return "unconfigured"

    def scan(self, max_messages=200):
        src = self.source()
        result = ScanResult(source=src)
        if src == "ms365":
            self._scan_ms365(result, max_messages)
        elif src == "imap":
            self._scan_imap(result, max_messages)
        elif src == "dumps":
            self._scan_dumps(result, max_messages)
        else:
            raise NotConfiguredError(
                "Email scan is not configured. Set Microsoft 365 credentials "
                "(MS365_TENANT_ID, MS365_CLIENT_ID, MS365_CLIENT_SECRET, "
                "MS365_USER) for OAuth via Microsoft Graph, or fall back to "
                "IMAP (EMAIL_IMAP_HOST / EMAIL_IMAP_USER / "
                "EMAIL_IMAP_PASSWORD), or drop sample .eml files in "
                f"{self.dumps_path()}. See "
                "integrations/examples/email_dump.example.eml for the body "
                "format and .env.example for the full set of env vars."
            )
        return result

    def _ms365_token(self):
        tenant = os.environ["MS365_TENANT_ID"]
        body = urllib.parse.urlencode({
            "client_id": os.environ["MS365_CLIENT_ID"],
            "client_secret": os.environ["MS365_CLIENT_SECRET"],
            "scope": os.environ.get("MS365_SCOPE", GRAPH_DEFAULT_SCOPE),
            "grant_type": "client_credentials",
        }).encode()
        req = urllib.request.Request(
            GRAPH_TOKEN_URL_FMT.format(tenant=urllib.parse.quote(tenant)),
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        token = payload.get("access_token")
        if not token:
            raise NotConfiguredError(
                f"Microsoft 365 token request failed: {payload!r}. Verify the "
                "app registration has Mail.Read (Application) permission with "
                "admin consent granted."
            )
        return token

    def _graph_get(self, url, token, accept="application/json"):
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": accept},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read(), resp.headers

    def _graph_patch(self, url, token, body):
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=30):
            pass

    def _scan_ms365(self, result, max_messages):
        token = self._ms365_token()
        user = urllib.parse.quote(os.environ["MS365_USER"])
        folder = urllib.parse.quote(os.environ.get("MS365_FOLDER", "Inbox"))
        mark_read = os.environ.get("MS365_MARK_READ") == "1"
        filt = os.environ.get("MS365_FILTER") or "isRead eq false"

        top = min(max_messages, 50)
        q = {
            "$top": str(top),
            "$select": "id,subject,from,receivedDateTime,isRead,hasAttachments",
            "$orderby": "receivedDateTime desc",
            "$filter": filt,
        }
        list_url = (f"{GRAPH_BASE}/users/{user}/mailFolders/{folder}/messages"
                    f"?{urllib.parse.urlencode(q)}")

        fetched = 0
        while list_url and fetched < max_messages:
            raw, _ = self._graph_get(list_url, token)
            page = json.loads(raw.decode("utf-8"))
            messages = page.get("value", [])
            for m in messages:
                if fetched >= max_messages:
                    break
                mid = m.get("id")
                if not mid:
                    continue
                fetched += 1
                result.messages_seen += 1
                mime_url = f"{GRAPH_BASE}/users/{user}/messages/{mid}/$value"
                try:
                    mime_bytes, _ = self._graph_get(mime_url, token, accept="text/plain")
                    msg = email.message_from_bytes(mime_bytes)
                    events, errs = parse_message_with_errors(msg)
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(
                        f"ms365 parse failed for {m.get('subject', mid)!r}: {exc}"
                    )
                    continue
                result.errors.extend(errs)
                if events:
                    result.messages_parsed += 1
                    result.events.extend(events)
                    if mark_read and not m.get("isRead"):
                        try:
                            self._graph_patch(
                                f"{GRAPH_BASE}/users/{user}/messages/{mid}",
                                token,
                                {"isRead": True},
                            )
                        except Exception as exc:  # noqa: BLE001
                            result.errors.append(
                                f"ms365 mark-read failed for {mid}: {exc}"
                            )
            list_url = page.get("@odata.nextLink")

    def _scan_imap(self, result, max_messages):
        host = os.environ["EMAIL_IMAP_HOST"]
        port = int(os.environ.get("EMAIL_IMAP_PORT", "993"))
        user = os.environ["EMAIL_IMAP_USER"]
        pw = os.environ["EMAIL_IMAP_PASSWORD"]
        folder = os.environ.get("EMAIL_IMAP_FOLDER", "INBOX")
        mark_read = os.environ.get("EMAIL_IMAP_MARK_READ") == "1"
        search = os.environ.get("EMAIL_IMAP_SEARCH")
        if not search:
            since = (datetime.utcnow() - timedelta(days=7)).strftime("%d-%b-%Y")
            search = f"(UNSEEN SINCE {since})"

        ctx = ssl.create_default_context()
        with imaplib.IMAP4_SSL(host, port, ssl_context=ctx) as imap:
            imap.login(user, pw)
            imap.select(folder)
            typ, data = imap.search(None, search)
            if typ != "OK":
                result.errors.append(f"IMAP search failed: {data!r}")
                return
            ids = (data[0] or b"").split()[:max_messages]
            result.messages_seen = len(ids)
            for msg_id in ids:
                typ, msg_data = imap.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                try:
                    msg = email.message_from_bytes(raw)
                    events, errs = parse_message_with_errors(msg)
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"parse failed for {msg_id!r}: {exc}")
                    continue
                result.errors.extend(errs)
                if events:
                    result.messages_parsed += 1
                    result.events.extend(events)
                    if not mark_read:
                        imap.store(msg_id, "-FLAGS", "\\Seen")

    def _scan_dumps(self, result, max_messages):
        paths = sorted(self.dumps_path().glob("*.eml"))[:max_messages]
        result.messages_seen = len(paths)
        for path in paths:
            try:
                with open(path, "rb") as f:
                    msg = email.message_from_bytes(f.read())
                events, errs = parse_message_with_errors(msg)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{path.name}: {exc}")
                continue
            result.errors.extend(errs)
            if events:
                result.messages_parsed += 1
                result.events.extend(events)


__all__ = [
    "EmailInboxClient",
    "EmailEvent",
    "ScanResult",
    "parse_message",
    "parse_message_with_errors",
]
        
