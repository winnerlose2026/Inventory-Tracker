"""FTPS inbox poller for distributor CSV drops.

Distributors (US Foods, Cheney Brothers) push two kinds of files to a
shared FTPS host (cPanel-hosted), each into their own per-account
chrooted directory:

    /incoming/inventory_YYYYMMDD.csv      -> on_hand events
                                              (live stock per DC, replaces qty)
    /incoming/shipments_YYYYMMDD.csv      -> restock events
                                              (carry a PO #; flow through the
                                               existing PO revision-replace)

This module is the pull side: it logs into the FTPS host as each
distributor account, lists the incoming dir, ingests anything it
hasn't seen before, hands the CSV bytes to the right parser, and
posts the resulting EmailEvents through the live web service's
``/api/email/ingest-events`` route. After a successful ingest the
file is moved to ``processed/`` so the inbox stays clean.

A cursor of "(account, filename, mtime, size)" tuples already
ingested is persisted at ``data/sftp_cursor.json`` (the persistent
disk attached to the Render service) so re-runs of this module are
idempotent even if a file lingers in incoming/ for any reason.

Configuration (env vars)
------------------------

    SFTP_HOST                   e.g. sftp.hhbagels.com
    SFTP_PORT                   default 21 (FTPS explicit)
    SFTP_USERNAME_USFOODS       cPanel FTP account for US Foods
    SFTP_PASSWORD_USFOODS       password for above
    SFTP_USERNAME_CHENEY        cPanel FTP account for Cheney
    SFTP_PASSWORD_CHENEY        password for above
    APP_URL                     https://bagel-inventory.onrender.com
    INVENTORY_API_TOKEN         required by /api/email/ingest-events
    SFTP_INCOMING_DIR           default "incoming"
    SFTP_PROCESSED_DIR          default "processed"
    SFTP_CURSOR_PATH            default data/sftp_cursor.json

Despite the variable names mentioning SFTP, the transport is FTPS
(port 21 with explicit TLS via ``ftplib.FTP_TLS``). cPanel only
supports a single SSH/SFTP user per account, but unlimited per-user
FTP accounts with their own chrooted home directories. FTPS is fine
for foodservice EDI; many distributors prefer it.
"""

from __future__ import annotations

import ftplib
import io
import json
import os
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_INCOMING = "incoming"
DEFAULT_PROCESSED = "processed"
DEFAULT_CURSOR_PATH = Path("data") / "sftp_cursor.json"

# Account name -> distributor display name. Account names match the
# cPanel FTP usernames JD will create. Keep the env var suffix in sync.
ACCOUNTS: list[tuple[str, str]] = [
    ("usfoods", "US Foods"),
    ("cheney",  "Cheney Brothers"),
]


@dataclass
class _RemoteFile:
    name: str
    size: int
    mtime: str   # ISO-ish "YYYYMMDDHHMMSS" string from MDTM


@dataclass
class IngestReport:
    account: str
    distributor: str
    files_seen: int = 0
    files_skipped: int = 0
    files_ingested: int = 0
    files_failed: int = 0
    server_reports: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Cursor persistence (already-ingested file fingerprints)
# ---------------------------------------------------------------------------

def _cursor_path() -> Path:
    raw = os.environ.get("SFTP_CURSOR_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_CURSOR_PATH


def _load_cursor() -> dict[str, list[str]]:
    """Returns {account: [fingerprint, ...]} where fingerprint = "name|size|mtime"."""
    p = _cursor_path()
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Normalize: every value should be a list[str]
    out: dict[str, list[str]] = {}
    for k, v in data.items():
        if isinstance(v, list):
            out[str(k)] = [str(x) for x in v]
    return out


def _save_cursor(cursor: dict[str, list[str]]) -> None:
    p = _cursor_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(cursor, f, indent=2, sort_keys=True)
    tmp.replace(p)


def _fingerprint(rf: _RemoteFile) -> str:
    return f"{rf.name}|{rf.size}|{rf.mtime}"


# ---------------------------------------------------------------------------
# FTPS transport
# ---------------------------------------------------------------------------

def _connect(host: str, port: int, user: str, password: str,
             timeout: int = 60) -> ftplib.FTP_TLS:
    ctx = ssl.create_default_context()
    # cPanel hosts often use a wildcard TLS cert; standard verification
    # works. If an operator needs to debug a self-signed cert we'll
    # surface the error rather than silently disable verification.
    ftp = ftplib.FTP_TLS(context=ctx, timeout=timeout)
    ftp.connect(host=host, port=port)
    ftp.auth()              # explicit TLS handshake
    ftp.login(user=user, passwd=password)
    ftp.prot_p()            # encrypt the data channel too
    return ftp


def _list_incoming(ftp: ftplib.FTP_TLS, incoming_dir: str) -> list[_RemoteFile]:
    # Try MLSD first (returns structured facts); fall back to NLST + MDTM.
    files: list[_RemoteFile] = []
    try:
        for name, facts in ftp.mlsd(path=incoming_dir):
            if facts.get("type") not in (None, "file"):
                continue
            if name in (".", ".."):
                continue
            size = int(facts.get("size") or 0)
            mtime = str(facts.get("modify") or "")
            files.append(_RemoteFile(name=name, size=size, mtime=mtime))
        return files
    except (ftplib.error_perm, AttributeError):
        pass

    # Fallback: NLST + per-file MDTM/SIZE
    try:
        names = ftp.nlst(incoming_dir)
    except ftplib.error_perm as exc:
        # Empty directory on some servers raises 550
        if "550" in str(exc):
            return []
        raise
    for full in names:
        name = full.split("/")[-1]
        if name in (".", ".."):
            continue
        size = 0
        mtime = ""
        try:
            ftp.voidcmd(f"TYPE I")
            size = ftp.size(f"{incoming_dir}/{name}") or 0
        except ftplib.error_perm:
            pass
        try:
            resp = ftp.voidcmd(f"MDTM {incoming_dir}/{name}")
            # "213 YYYYMMDDHHMMSS"
            mtime = resp.split(" ", 1)[-1].strip()
        except ftplib.error_perm:
            pass
        files.append(_RemoteFile(name=name, size=size, mtime=mtime))
    return files


def _download_bytes(ftp: ftplib.FTP_TLS, remote_path: str) -> bytes:
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {remote_path}", buf.write)
    return buf.getvalue()


def _ensure_dir(ftp: ftplib.FTP_TLS, path: str) -> None:
    try:
        ftp.mkd(path)
    except ftplib.error_perm as exc:
        # 550 already exists is fine
        if "550" not in str(exc):
            raise


def _move_to_processed(ftp: ftplib.FTP_TLS, src_path: str,
                       processed_dir: str, name: str) -> None:
    _ensure_dir(ftp, processed_dir)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dest = f"{processed_dir}/{stamp}_{name}"
    try:
        ftp.rename(src_path, dest)
    except ftplib.error_perm:
        # If rename fails (cross-device or perms), fall back to leaving
        # in place — the cursor still prevents re-ingestion.
        pass


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

def classify(name: str) -> Optional[str]:
    """Return ``inventory`` | ``shipments`` | ``None`` based on filename.

    Lower-cases the name and looks for a substring; this tolerates
    weird date suffixes and the difference between e.g. ``inventory.csv``
    and ``Inventory_2026-05-01.csv``.
    """
    n = name.lower()
    if not n.endswith((".csv", ".txt")):
        return None
    if "shipment" in n or "shipping" in n or "movement" in n or "867" in n:
        return "shipments"
    if "inventory" in n or "on_hand" in n or "onhand" in n or "846" in n:
        return "inventory"
    return None


# ---------------------------------------------------------------------------
# Posting to the web service
# ---------------------------------------------------------------------------

def _post_events(events: list[dict], source: str,
                 messages_seen: int, messages_parsed: int,
                 errors: list[str], dry_run: bool) -> dict:
    """POST a payload to /api/email/ingest-events. Returns the parsed
    JSON response (or a synthesized error dict)."""
    app_url = os.environ.get("APP_URL", "").strip().rstrip("/")
    token = os.environ.get("INVENTORY_API_TOKEN", "").strip()
    if not app_url:
        return {"ok": False, "error": "APP_URL not set"}

    payload = {
        "dry_run": dry_run,
        "source": source,
        "messages_seen": messages_seen,
        "messages_parsed": messages_parsed,
        "errors": errors,
        "events": events,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Inventory-Token"] = token
    req = urllib.request.Request(
        f"{app_url}/api/email/ingest-events",
        data=body, method="POST", headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "error": f"HTTP {exc.code} {exc.reason}",
            "body": exc.read().decode("utf-8", errors="replace")[:500],
        }
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"URLError: {exc.reason}"}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _parse_file(distributor: str, kind: str, name: str,
                content: bytes) -> tuple[list[dict], list[str]]:
    """Hand off to the right parser. Returns (events, errors)."""
    from .parsers import (
        parse_inventory_csv,
        parse_shipments_csv,
    )
    if kind == "inventory":
        return parse_inventory_csv(distributor, name, content)
    if kind == "shipments":
        return parse_shipments_csv(distributor, name, content)
    return [], [f"{name}: unknown kind {kind!r}"]


def pull_account(account: str, distributor: str, *,
                 dry_run: bool = False,
                 cursor: Optional[dict[str, list[str]]] = None,
                 ) -> IngestReport:
    report = IngestReport(account=account, distributor=distributor)

    host = os.environ.get("SFTP_HOST", "").strip()
    port = int(os.environ.get("SFTP_PORT", "21").strip() or "21")
    user = os.environ.get(f"SFTP_USERNAME_{account.upper()}", "").strip()
    pwd  = os.environ.get(f"SFTP_PASSWORD_{account.upper()}", "").strip()
    incoming = os.environ.get("SFTP_INCOMING_DIR", DEFAULT_INCOMING).strip() or DEFAULT_INCOMING
    processed = os.environ.get("SFTP_PROCESSED_DIR", DEFAULT_PROCESSED).strip() or DEFAULT_PROCESSED

    if not host or not user or not pwd:
        report.errors.append(
            f"missing SFTP_HOST / SFTP_USERNAME_{account.upper()} / "
            f"SFTP_PASSWORD_{account.upper()}"
        )
        return report

    cursor = cursor if cursor is not None else _load_cursor()
    seen_set = set(cursor.get(account) or [])

    try:
        ftp = _connect(host=host, port=port, user=user, password=pwd)
    except (ftplib.all_errors, socket.error, ssl.SSLError, OSError) as exc:
        report.errors.append(f"connect failed: {type(exc).__name__}: {exc}")
        return report

    try:
        files = _list_incoming(ftp, incoming)
        report.files_seen = len(files)

        new_fingerprints: list[str] = []
        for rf in files:
            fp = _fingerprint(rf)
            if fp in seen_set:
                report.files_skipped += 1
                continue

            kind = classify(rf.name)
            if kind is None:
                report.files_skipped += 1
                report.errors.append(f"{rf.name}: skipped (unrecognized filename)")
                continue

            try:
                content = _download_bytes(ftp, f"{incoming}/{rf.name}")
            except (ftplib.all_errors, OSError) as exc:
                report.files_failed += 1
                report.errors.append(f"{rf.name}: download failed: {exc}")
                continue

            events, parse_errors = _parse_file(distributor, kind, rf.name, content)
            for err in parse_errors:
                report.errors.append(f"{rf.name}: {err}")
            if not events:
                report.files_failed += 1
                report.errors.append(f"{rf.name}: produced 0 events")
                continue

            resp = _post_events(
                events=events,
                source=f"sftp-inbox/{account}/{rf.name}",
                messages_seen=1,
                messages_parsed=1,
                errors=[],
                dry_run=dry_run,
            )
            report.server_reports.append({
                "file": rf.name,
                "kind": kind,
                "events": len(events),
                "response": resp,
            })

            ok = (resp.get("ok") is not False
                  and "error" not in resp
                  and resp.get("reports", [{}])[0].get("status") == "ok")
            if ok:
                report.files_ingested += 1
                if not dry_run:
                    _move_to_processed(ftp, f"{incoming}/{rf.name}",
                                       processed, rf.name)
                new_fingerprints.append(fp)
            else:
                report.files_failed += 1

        # Persist cursor
        if new_fingerprints and not dry_run:
            cursor.setdefault(account, [])
            cursor[account] = sorted(set(cursor[account]) | set(new_fingerprints))
            _save_cursor(cursor)
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            try:
                ftp.close()
            except Exception:  # noqa: BLE001
                pass

    return report


def pull_all(*, dry_run: bool = False) -> list[IngestReport]:
    cursor = _load_cursor()
    out: list[IngestReport] = []
    for account, distributor in ACCOUNTS:
        out.append(pull_account(account, distributor,
                                dry_run=dry_run, cursor=cursor))
    return out
