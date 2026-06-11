"""Microsoft Graph change-notification subscriptions for Daily Production.

The 6-hour Render cron pulls the mailbox on a schedule. That's fine for
backfill but means a Daily Production email can sit unprocessed for hours.
This module lets the app subscribe to Graph "new message" notifications so
each Daily Production sheet is parsed seconds after it lands in the inbox.

Flow
----
1. ``create_subscriptions()`` POSTs to ``/subscriptions`` for every mailbox
   in ``MS365_USER``. Graph immediately calls our webhook with a
   ``validationToken`` to confirm the endpoint is live; the webhook returns
   the token verbatim. Each subscription's ID + expiration is recorded in
   ``data/graph_subscriptions.json``.
2. When a new message arrives, Graph POSTs a notification to our webhook.
   The webhook fetches the message via Graph, qualifies it (sender on
   ``hhbagels.com`` + subject contains "Daily Production"), pulls the first
   PDF attachment, and ingests it through the existing
   ``/api/production/ingest`` parse path.
3. Mail subscriptions cap at ~70 hours, so ``renew_subscriptions()`` PATCHes
   each one with a fresh ``expirationDateTime``. A Render cron calls the
   renew endpoint daily; if the subscription is gone, we recreate it.

Env vars
--------
``MS365_TENANT_ID`` / ``MS365_CLIENT_ID`` / ``MS365_CLIENT_SECRET`` —
    same Entra app already used by the email scanner. The app registration
    needs ``Mail.Read`` (application) **already granted**, which it has.
``MS365_USER`` —
    comma-separated UPN list (mailboxes to watch).
``MS365_FOLDER`` —
    folder name (defaults to ``Inbox``).
``GRAPH_WEBHOOK_BASE`` —
    public HTTPS base URL of the Render service, e.g.
    ``https://bagel-inventory.onrender.com``. Used to compose the
    notification URL Graph will POST to.
``GRAPH_SUB_CLIENT_STATE`` —
    arbitrary secret string. Graph echoes this back on every notification
    so we can confirm the call is genuinely from our subscription. Treat
    it like a token — set it via Render env, don't commit it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .email_scanner import EmailInboxClient, GRAPH_BASE


# Mail change-notification subscriptions max out at 4230 minutes (~70.5h).
# We renew well before that — 68h asked, daily cron PATCHes back up to 68h.
SUBSCRIPTION_LIFETIME_HOURS = 68

# Where we persist subscription metadata. Lives alongside inventory.json
# on the Render persistent disk so it survives cold starts.
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_STATE_FILE = _DATA_DIR / "graph_subscriptions.json"


# --------------------------------------------------------------------------- #
# State persistence
# --------------------------------------------------------------------------- #

def _load_state() -> dict:
    if not _STATE_FILE.exists():
        return {"subscriptions": []}
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"subscriptions": []}


def _save_state(state: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def list_subscriptions() -> list[dict]:
    """Return the locally-tracked subscription records (id, user, expiry)."""
    return list(_load_state().get("subscriptions") or [])


# --------------------------------------------------------------------------- #
# Graph helpers
# --------------------------------------------------------------------------- #

def _graph_request(
    method: str,
    url: str,
    token: str,
    body: Optional[dict] = None,
) -> dict:
    """Issue an authenticated Graph request and return parsed JSON.

    Surfaces the Graph error code + message on failure so 401/403/404 are
    distinguishable from token failures.
    """
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    _host = (urllib.parse.urlparse(url).hostname or "").lower()
    if _host not in ("graph.microsoft.com", "login.microsoftonline.com"):
        raise ValueError("refusing outbound request to untrusted host")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(
            f"Graph {method} {url} failed: {exc.code} {exc.reason}: "
            f"{body_text[:500]}"
        ) from exc


def _users() -> list[str]:
    raw = os.environ.get("MS365_USER", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def _folder() -> str:
    return os.environ.get("MS365_FOLDER", "Inbox")


def _webhook_url() -> str:
    base = (os.environ.get("GRAPH_WEBHOOK_BASE") or "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            "GRAPH_WEBHOOK_BASE is not set. Set it to the public HTTPS URL "
            "of this Render service, e.g. https://bagel-inventory.onrender.com"
        )
    if not base.startswith("https://"):
        raise RuntimeError(
            f"GRAPH_WEBHOOK_BASE must start with https:// (got {base!r}). "
            "Graph rejects http and self-signed certs."
        )
    return f"{base}/webhooks/graph/notifications"


def _client_state() -> str:
    cs = (os.environ.get("GRAPH_SUB_CLIENT_STATE") or "").strip()
    if not cs:
        raise RuntimeError(
            "GRAPH_SUB_CLIENT_STATE is not set. Pick a secret string "
            "(treated like a token) and set it via Render env vars."
        )
    return cs


def _expiration_iso(hours: int = SUBSCRIPTION_LIFETIME_HOURS) -> str:
    when = datetime.now(timezone.utc) + timedelta(hours=hours)
    # Graph wants 8601 with Z, no microseconds.
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resource_for(user: str) -> str:
    # Subscribing to a specific folder narrows the noise. The folder name is
    # URL-encoded inside the OData literal.
    folder = _folder().replace("'", "''")
    return f"users/{user}/mailFolders('{folder}')/messages"


# --------------------------------------------------------------------------- #
# Public API: create / renew / delete
# --------------------------------------------------------------------------- #

def create_subscriptions() -> dict:
    """Create a subscription per mailbox in MS365_USER.

    Idempotent in the sense that existing local subscription records for the
    same mailbox are *replaced* — Graph will end up with two live subscriptions
    if you call this twice without renewing, but the duplicate just sends
    duplicate notifications which the production ingest path dedupes by
    message id.
    """
    client = EmailInboxClient()
    token = client._ms365_token()

    notification_url = _webhook_url()
    client_state = _client_state()
    expiration = _expiration_iso()

    state = _load_state()
    by_user = {s.get("user"): s for s in state.get("subscriptions") or []}
    results = []

    for user in _users():
        body = {
            "changeType": "created",
            "notificationUrl": notification_url,
            "resource": _resource_for(user),
            "expirationDateTime": expiration,
            "clientState": client_state,
        }
        try:
            resp = _graph_request(
                "POST", f"{GRAPH_BASE}/subscriptions", token, body=body,
            )
        except Exception as exc:  # noqa: BLE001
            results.append({"user": user, "ok": False, "error": str(exc)})
            continue
        rec = {
            "id": resp.get("id"),
            "user": user,
            "resource": resp.get("resource"),
            "expiration_date_time": resp.get("expirationDateTime"),
            "notification_url": notification_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        by_user[user] = rec
        results.append({"user": user, "ok": True, "subscription": rec})

    state["subscriptions"] = list(by_user.values())
    _save_state(state)
    return {"ok": True, "results": results, "subscriptions": state["subscriptions"]}


def renew_subscriptions() -> dict:
    """PATCH each tracked subscription with a fresh expirationDateTime.

    If Graph reports the subscription is gone (404), recreate it for that
    mailbox so we don't silently lose coverage.
    """
    client = EmailInboxClient()
    token = client._ms365_token()

    state = _load_state()
    subs = list(state.get("subscriptions") or [])

    # Cover any new mailboxes added to MS365_USER since the last create.
    tracked_users = {s.get("user") for s in subs}
    missing_users = [u for u in _users() if u not in tracked_users]

    new_expiration = _expiration_iso()
    results = []
    by_user = {s.get("user"): s for s in subs}

    for sub in subs:
        sub_id = sub.get("id")
        user = sub.get("user")
        if not sub_id:
            results.append({"user": user, "ok": False, "error": "missing id"})
            continue
        try:
            resp = _graph_request(
                "PATCH",
                f"{GRAPH_BASE}/subscriptions/{urllib.parse.quote(sub_id)}",
                token,
                body={"expirationDateTime": new_expiration},
            )
            sub["expiration_date_time"] = resp.get(
                "expirationDateTime", new_expiration,
            )
            sub["renewed_at"] = datetime.now(timezone.utc).isoformat()
            by_user[user] = sub
            results.append({"user": user, "ok": True, "subscription": sub})
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "404" in msg or "ResourceNotFound" in msg:
                # Subscription expired or was deleted server-side. Recreate.
                try:
                    body = {
                        "changeType": "created",
                        "notificationUrl": _webhook_url(),
                        "resource": _resource_for(user),
                        "expirationDateTime": new_expiration,
                        "clientState": _client_state(),
                    }
                    created = _graph_request(
                        "POST", f"{GRAPH_BASE}/subscriptions", token, body=body,
                    )
                    rec = {
                        "id": created.get("id"),
                        "user": user,
                        "resource": created.get("resource"),
                        "expiration_date_time": created.get("expirationDateTime"),
                        "notification_url": _webhook_url(),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "recreated_from": sub_id,
                    }
                    by_user[user] = rec
                    results.append({
                        "user": user, "ok": True, "subscription": rec,
                        "note": "recreated after 404",
                    })
                except Exception as exc2:  # noqa: BLE001
                    results.append({
                        "user": user, "ok": False,
                        "error": f"renew 404, recreate failed: {exc2}",
                    })
            else:
                results.append({"user": user, "ok": False, "error": msg})

    # Create subscriptions for mailboxes we don't yet cover.
    for user in missing_users:
        body = {
            "changeType": "created",
            "notificationUrl": _webhook_url(),
            "resource": _resource_for(user),
            "expirationDateTime": new_expiration,
            "clientState": _client_state(),
        }
        try:
            created = _graph_request(
                "POST", f"{GRAPH_BASE}/subscriptions", token, body=body,
            )
            rec = {
                "id": created.get("id"),
                "user": user,
                "resource": created.get("resource"),
                "expiration_date_time": created.get("expirationDateTime"),
                "notification_url": _webhook_url(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            by_user[user] = rec
            results.append({
                "user": user, "ok": True, "subscription": rec,
                "note": "newly covered mailbox",
            })
        except Exception as exc:  # noqa: BLE001
            results.append({"user": user, "ok": False, "error": str(exc)})

    state["subscriptions"] = list(by_user.values())
    _save_state(state)
    return {"ok": True, "results": results, "subscriptions": state["subscriptions"]}


def delete_subscription(sub_id: str) -> dict:
    """Delete a specific Graph subscription and drop it from local state."""
    client = EmailInboxClient()
    token = client._ms365_token()
    err = None
    try:
        _graph_request(
            "DELETE",
            f"{GRAPH_BASE}/subscriptions/{urllib.parse.quote(sub_id)}",
            token,
        )
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    state = _load_state()
    state["subscriptions"] = [
        s for s in state.get("subscriptions") or [] if s.get("id") != sub_id
    ]
    _save_state(state)
    return {"ok": err is None, "error": err}


# --------------------------------------------------------------------------- #
# Notification processing
# --------------------------------------------------------------------------- #

def _fetch_message(user: str, message_graph_id: str, token: str) -> dict:
    """Fetch a single message by its Graph id (NOT internetMessageId)."""
    url = (
        f"{GRAPH_BASE}/users/{urllib.parse.quote(user)}/messages/"
        f"{urllib.parse.quote(message_graph_id)}"
        "?$select=id,subject,from,receivedDateTime,hasAttachments,internetMessageId"
    )
    return _graph_request("GET", url, token)


def _fetch_first_pdf(user: str, message_graph_id: str, token: str) -> Optional[bytes]:
    """Return the bytes of the first PDF attachment on the message, if any."""
    import base64
    list_url = (
        f"{GRAPH_BASE}/users/{urllib.parse.quote(user)}/messages/"
        f"{urllib.parse.quote(message_graph_id)}/attachments"
    )
    page = _graph_request("GET", list_url, token)
    for a in page.get("value", []):
        name = (a.get("name") or "").lower()
        ctype = (a.get("contentType") or "").lower()
        if name.endswith(".pdf") or ctype == "application/pdf":
            acid = a.get("id")
            fetch_url = (
                f"{GRAPH_BASE}/users/{urllib.parse.quote(user)}/messages/"
                f"{urllib.parse.quote(message_graph_id)}/attachments/"
                f"{urllib.parse.quote(acid)}"
            )
            payload = _graph_request("GET", fetch_url, token)
            content_b64 = payload.get("contentBytes") or ""
            try:
                return base64.b64decode(content_b64)
            except Exception:  # noqa: BLE001
                return None
    return None


def _user_from_resource(resource: str) -> Optional[str]:
    """Pull the UPN out of "Users/<upn>/MailFolders('Inbox')/Messages/<id>"."""
    if not resource:
        return None
    # Graph's resource path is mixed-case; normalize for parsing.
    parts = resource.replace("\\", "/").split("/")
    for i, p in enumerate(parts):
        if p.lower() == "users" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def _message_id_from_resource(resource: str) -> Optional[str]:
    """Pull the message Graph id out of the resource path."""
    if not resource:
        return None
    parts = resource.split("/")
    for i, p in enumerate(parts):
        if p.lower() == "messages" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def handle_notification(payload: dict) -> dict:
    """Process a Graph change-notification batch.

    Validates ``clientState`` on each entry, then for every notification
    that names a message resource:
      - fetch the message header from Graph
      - qualify it (sender on hhbagels.com + subject contains
        "daily production")
      - download the first PDF attachment
      - ingest via the existing production parse path (idempotent on
        internetMessageId)

    Returns a summary dict for the response body / logs. Always returns
    HTTP-200-safe output; the Flask layer returns 202 regardless so Graph
    keeps the subscription healthy.
    """
    from inventory_tracker import load_production, save_production
    from .production_pdf_parser import parse_production_pdf

    expected_client_state = _client_state()

    notifications = payload.get("value") or []
    if not isinstance(notifications, list):
        return {"ok": False, "error": "value field is not a list"}

    client = EmailInboxClient()
    try:
        token = client._ms365_token()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"token failed: {exc}"}

    ingested = 0
    skipped = 0
    errors: list[str] = []
    summary: list[dict] = []
    records = load_production()
    seen_ids = {r.get("source_message_id") for r in records if r.get("source_message_id")}

    for note in notifications:
        cs = note.get("clientState") or ""
        if cs != expected_client_state:
            errors.append("clientState mismatch — notification rejected")
            continue
        resource = note.get("resource") or note.get("resourceData", {}).get("@odata.id") or ""
        user = _user_from_resource(resource) or ""
        msg_id = _message_id_from_resource(resource)
        if not (user and msg_id):
            # Some notifications (e.g. validation echo) won't have a message
            # id — those are fine to ignore.
            skipped += 1
            continue
        try:
            msg = _fetch_message(user, msg_id, token)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"fetch {msg_id}: {exc}")
            continue

        subject = msg.get("subject") or ""
        sender = (((msg.get("from") or {}).get("emailAddress") or {})
                  .get("address") or "")
        received_at = msg.get("receivedDateTime") or ""
        internet_id = msg.get("internetMessageId") or msg.get("id") or msg_id

        dom = sender.split("@")[-1].lower() if "@" in sender else ""
        if not (dom == "hhbagels.com" or dom.endswith(".hhbagels.com")):
            skipped += 1
            continue
        if "daily production" not in subject.lower():
            skipped += 1
            continue

        if internet_id in seen_ids:
            summary.append({"subject": subject, "status": "duplicate"})
            skipped += 1
            continue

        if not msg.get("hasAttachments"):
            errors.append(f"{subject[:60]!r}: no attachments")
            continue

        try:
            pdf_bytes = _fetch_first_pdf(user, msg_id, token)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{subject[:60]!r}: fetch-att failed: {exc}")
            continue
        if not pdf_bytes:
            errors.append(f"{subject[:60]!r}: no PDF attachment")
            continue

        try:
            sheet = parse_production_pdf(pdf_bytes, subject=subject)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{subject[:60]!r}: parse failed: {exc}")
            continue

        now_iso = datetime.now(timezone.utc).isoformat()
        if sheet.error and not sheet.lines:
            stub = {
                "production_date": "",
                "warehouse": "",
                "warehouse_raw": "",
                "distributor": "",
                "po_number": "",
                "lines": [],
                "total_cases": 0,
                "unmapped_varieties": [],
                "source_message_id": internet_id,
                "source_subject": subject,
                "source_sender": sender,
                "received_at": received_at or now_iso,
                "ingested_at": now_iso,
                "parse_error": sheet.error,
            }
            records.append(stub)
            seen_ids.add(internet_id)
            summary.append({"subject": subject, "status": "parse_error",
                            "error": sheet.error})
            errors.append(f"{subject[:60]!r}: {sheet.error}")
            continue

        record = {
            "production_date":    sheet.production_date,
            "warehouse":          sheet.warehouse,
            "warehouse_raw":      sheet.warehouse_raw,
            "distributor":        sheet.distributor,
            "po_number":          sheet.po_number,
            "lines": [
                {"variety": L.variety, "raw_variety": L.raw_variety,
                 "cs_count": L.cs_count, "lot_number": L.lot_number}
                for L in sheet.lines
            ],
            "total_cases":        sheet.total_cases,
            "unmapped_varieties": sheet.unmapped_varieties,
            "source_message_id":  internet_id,
            "source_subject":     subject,
            "source_sender":      sender,
            "received_at":        received_at or now_iso,
            "ingested_at":        now_iso,
            "parse_error":        "",
        }
        records.append(record)
        seen_ids.add(internet_id)
        ingested += 1
        summary.append({
            "subject": subject,
            "status": "ingested",
            "production_date": sheet.production_date,
            "warehouse": sheet.warehouse,
            "total_cases": sheet.total_cases,
        })

    if ingested or any(s.get("status") == "parse_error" for s in summary):
        save_production(records)

    return {
        "ok": True,
        "received": len(notifications),
        "ingested": ingested,
        "skipped": skipped,
        "errors": errors,
        "summary": summary,
    }
