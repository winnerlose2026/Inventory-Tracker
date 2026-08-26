#!/usr/bin/env python3
"""Daily sync of Cheney Brothers' data feeds into the tracker.

The receiving side lives on Render (POST /api/ingest/cheney-inventory-csv);
this is the *bridge* that pulls Cheney's daily drop and hands it over.
Runs equally well as a Render Cron Job or a scheduled Cowork routine.

Feeds (agreed with Walt Wilcox / Cheney, 2026-07-06):
  * Daily on-hand inventory CSV  -> POSTed to the tracker (applied as on_hand)
  * Daily EDI 810 invoices       -> parsed + summarized (WHERE the 810 lands in
                                    the tracker is still a modelling decision;
                                    the field semantics are settled)

2026-08-04 -- the first real CSV drop was an "OrderGuide" export, NOT the
on-hand snapshot: catalog + case cost, with the on-hand column zero on every
row (see integrations/cheney_order_guide.py). Order-guide files are therefore
detected by shape and reported WITHOUT being POSTed to the on-hand endpoint --
posting them would zero out every warehouse's real count. If Cheney later adds
a genuine on-hand file to the same drop it flows through untouched, because the
routing is by file shape, not by filename.

2026-08-26 -- TRANSPORT. The landing is the GoDaddy cPanel host we already pay
for. cPanel gives one SSH/SFTP user per hosting account but unlimited FTP
accounts with their own chrooted homes, so a distributor gets FTPS (explicit
TLS on 21), not SSH-SFTP. This bridge therefore defaults to FTPS and reuses the
single FTPS client in integrations/sftp_inbox.py (which carries the
shared-hosting TLS workaround). SSH-SFTP is still supported for any future
landing that offers it -- set CHENEY_FEED_TRANSPORT=sftp or a private key.

CONFIG (all via env; the script no-ops quietly until the drop is configured).
The CHENEY_* names win; the shared SFTP_* names used by sftp_inbox.py are
accepted as fallbacks so the cPanel account only has to be configured once.

  CHENEY_FEED_TRANSPORT   "ftps" (default) or "sftp"
  CHENEY_SFTP_HOST        hostname                    (or SFTP_HOST)
  CHENEY_SFTP_PORT        default 21 for ftps, 22 for sftp
  CHENEY_SFTP_USER        username           (or SFTP_USERNAME_CHENEY)
  CHENEY_SFTP_PASSWORD    password           (or SFTP_PASSWORD_CHENEY)
  CHENEY_SFTP_KEY         private key path -- SSH-SFTP only, implies sftp
  CHENEY_SFTP_DIR         dir to scan  (or SFTP_INCOMING_DIR, default
                          "incoming"; "." = the login directory)
  CHENEY_SFTP_PROCESSED_DIR   handled files are filed here after a
                          successful --commit run (or SFTP_PROCESSED_DIR,
                          default "processed"). Set to "" to leave files
                          in place -- note the drop then grows without
                          bound and every run re-downloads all of it.
  CHENEY_SFTP_CSV_GLOB    default "*.csv"
  CHENEY_SFTP_810_GLOB    default "*.edi,*.810,*.x12,*.txt"
  INVENTORY_API_TOKEN     tracker API token (X-Inventory-Token)
  APP_URL / INVENTORY_API_BASE   tracker base URL

Usage:
  python scripts/sync_cheney_feeds.py            # dry run (default): pull + report
  python scripts/sync_cheney_feeds.py --commit   # apply the CSV feed, file the drop

The FTPS transport needs nothing beyond the standard library. The SSH-SFTP
transport needs paramiko (pip install paramiko); it is imported lazily so the
rest of the repo and the test suite never depend on it.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import posixpath
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_API = (os.environ.get("APP_URL") or os.environ.get("INVENTORY_API_BASE")
               or "https://bagel-inventory.onrender.com").rstrip("/")


def _env(*names: str, default: str = "") -> str:
    """First non-empty value among ``names``, else ``default``."""
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return default


def _host() -> str:
    return _env("CHENEY_SFTP_HOST", "SFTP_HOST")


def _user() -> str:
    return _env("CHENEY_SFTP_USER", "SFTP_USERNAME_CHENEY")


def _password() -> str:
    return _env("CHENEY_SFTP_PASSWORD", "SFTP_PASSWORD_CHENEY")


def _feed_configured() -> bool:
    return bool(_host() and _user())


def _transport_name() -> str:
    """``ftps`` (default) or ``sftp``.

    An explicit CHENEY_FEED_TRANSPORT wins. Otherwise a private key implies
    SSH-SFTP (FTPS has no notion of one), as does an explicit port 22.
    """
    explicit = _env("CHENEY_FEED_TRANSPORT").lower()
    if explicit in ("ftps", "sftp"):
        return explicit
    if explicit:
        raise SystemExit(f"CHENEY_FEED_TRANSPORT must be 'ftps' or 'sftp', got {explicit!r}")
    if _env("CHENEY_SFTP_KEY"):
        return "sftp"
    if _env("CHENEY_SFTP_PORT") == "22":
        return "sftp"
    return "ftps"


class _FtpsTransport:
    """cPanel-style FTPS, explicit TLS on 21. Stdlib only."""

    scheme = "ftps"

    def __init__(self, host: str, port: int, user: str, password: str):
        from integrations.sftp_inbox import connect_ftps
        self._ftp = connect_ftps(host=host, port=port, user=user, password=password)
        self.label = f"ftps://{user}@{host}:{port}"

    def listdir(self, path: str) -> list[str]:
        from integrations.sftp_inbox import list_remote_files
        return [rf.name for rf in list_remote_files(self._ftp, path)]

    def download(self, remote_path: str) -> bytes:
        from integrations.sftp_inbox import download_bytes
        return download_bytes(self._ftp, remote_path)

    def file_away(self, remote_dir: str, name: str, processed_dir: str) -> None:
        from integrations.sftp_inbox import move_to_processed
        move_to_processed(self._ftp, posixpath.join(remote_dir, name),
                          processed_dir, name)

    def close(self) -> None:
        try:
            self._ftp.quit()
        except Exception:  # noqa: BLE001 - closing is best-effort
            try:
                self._ftp.close()
            except Exception:  # noqa: BLE001
                pass


class _SftpTransport:
    """Real SSH-SFTP, for a landing that can give us our own SSH user."""

    scheme = "sftp"

    def __init__(self, host: str, port: int, user: str, password: str,
                 key_path: str = ""):
        try:
            import paramiko
        except ImportError:
            print("ERROR: paramiko not installed (pip install paramiko), and "
                  "CHENEY_FEED_TRANSPORT=sftp needs it. The cPanel landing "
                  "uses FTPS, which needs no extra package.", file=sys.stderr)
            raise SystemExit(3)
        self._transport = paramiko.Transport((host, port))
        if key_path:
            self._transport.connect(username=user,
                                    pkey=paramiko.RSAKey.from_private_key_file(key_path))
        else:
            self._transport.connect(username=user, password=password)
        self._client = paramiko.SFTPClient.from_transport(self._transport)
        self.label = f"sftp://{user}@{host}:{port}"

    def listdir(self, path: str) -> list[str]:
        return list(self._client.listdir(path))

    def download(self, remote_path: str) -> bytes:
        with self._client.open(remote_path, "rb") as fh:
            return fh.read()

    def file_away(self, remote_dir: str, name: str, processed_dir: str) -> None:
        from datetime import datetime
        try:
            self._client.mkdir(processed_dir)
        except OSError:
            pass  # already there
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        try:
            self._client.rename(posixpath.join(remote_dir, name),
                                posixpath.join(processed_dir, f"{stamp}_{name}"))
        except OSError:
            pass  # leave in place; a re-read is harmless

    def close(self) -> None:
        for closer in (getattr(self, "_client", None), getattr(self, "_transport", None)):
            try:
                closer.close()
            except Exception:  # noqa: BLE001
                pass


def _open_transport():
    host, user, pw = _host(), _user(), _password()
    name = _transport_name()
    port = int(_env("CHENEY_SFTP_PORT", default="21" if name == "ftps" else "22"))
    if name == "sftp":
        return _SftpTransport(host, port, user, pw, _env("CHENEY_SFTP_KEY"))
    return _FtpsTransport(host, port, user, pw)


def _matches(name, globs):
    return any(fnmatch.fnmatch(name.lower(), g.strip().lower()) for g in globs if g.strip())


def _post(url, data_bytes, token, content_type="text/csv"):
    headers = {"Content-Type": content_type}
    if token:
        headers["X-Inventory-Token"] = token
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--api-base", default=DEFAULT_API)
    p.add_argument("--token", default=os.environ.get("INVENTORY_API_TOKEN"))
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--commit", dest="dry_run", action="store_false",
                   help="Apply the CSV feed (default is dry run)")
    args = p.parse_args()

    if not _feed_configured():
        print("Cheney feed not configured yet (set CHENEY_SFTP_HOST/USER/... "
              "or SFTP_HOST + SFTP_USERNAME_CHENEY/SFTP_PASSWORD_CHENEY). "
              "Nothing to do -- exiting cleanly.")
        return 0

    csv_globs = (_env("CHENEY_SFTP_CSV_GLOB", default="*.csv")).split(",")
    edi_globs = (_env("CHENEY_SFTP_810_GLOB",
                      default="*.edi,*.810,*.x12,*.txt")).split(",")
    remote_dir = _env("CHENEY_SFTP_DIR", "SFTP_INCOMING_DIR", default="incoming")
    processed_dir = os.environ.get(
        "CHENEY_SFTP_PROCESSED_DIR",
        os.environ.get("SFTP_PROCESSED_DIR", "processed"),
    ).strip()

    conn = _open_transport()
    tmp = Path(tempfile.mkdtemp(prefix="cheney_feed_"))
    csv_files: list[Path] = []
    edi_files: list[Path] = []
    handled: list[str] = []
    try:
        print(f"Connected to {conn.label}, scanning {remote_dir!r}")
        for name in sorted(conn.listdir(remote_dir)):
            if _matches(name, csv_globs):
                bucket = csv_files
            elif _matches(name, edi_globs):
                bucket = edi_files
            else:
                continue
            local = tmp / name
            local.write_bytes(conn.download(posixpath.join(remote_dir, name)))
            bucket.append(local)

        print(f"Pulled {len(csv_files)} CSV + {len(edi_files)} EDI file(s) "
              f"from {remote_dir} over {conn.scheme.upper()}")

        # --- CSVs: route by shape, never post an order guide to the on-hand path ---
        from integrations.cheney_order_guide import (
            parse_order_guide, looks_like_order_guide, summarize as og_summarize)

        dry = "1" if args.dry_run else "0"
        guides = onhand = 0
        for lp in sorted(csv_files):
            text = lp.read_text(encoding="utf-8", errors="replace")
            if looks_like_order_guide(text):
                rows, errors, meta = parse_order_guide(text, filename=lp.name)
                s = og_summarize(rows, meta)
                where = ", ".join(s["warehouses"]) or ", ".join(s["out_of_scope_dc"]) or "?"
                if not meta["on_hand_populated"]:
                    guides += 1
                    handled.append(lp.name)
                    print(f"  ORDER GUIDE {lp.name}: {s['rows']} row(s) for "
                          f"{s['store']} ({where}); {s['priced_rows']} priced, "
                          f"{len(s['hh_varieties'])} H&H variet(y/ies). "
                          f"NOT applied to on-hand (no on-hand data in this file).")
                    for e in errors[:3]:
                        print(f"      ! {e}")
                    continue
                # Same layout, but the on-hand column IS populated -- this is the
                # snapshot we've been waiting on. POST it; the receiver routes it
                # through cheney_csv_inventory, which converts it.
                print(f"  ORDER GUIDE + ON-HAND {lp.name}: {s['rows']} row(s) for "
                      f"{s['store']} ({where}); on-hand IS populated "
                      f"({meta['on_hand_nonzero_rows']} non-zero row(s)) -- "
                      f"applying as a snapshot.")
            onhand += 1
            url = f"{args.api_base}/api/ingest/cheney-inventory-csv?dry_run={dry}&filename={lp.name}"
            status, resp = _post(url, lp.read_bytes(), args.token, "text/csv")
            print(f"  CSV {lp.name}: POST -> HTTP {status}  {resp[:200]}")
            if status == 200:
                handled.append(lp.name)

        # --- EDI 810 -> parse + summarize only (tracker routing still deferred) ---
        if edi_files:
            from integrations.edi_810 import parse_810, summarize as edi_summarize
            invoices: list[dict] = []
            for lp in sorted(edi_files):
                inv = parse_810(lp.read_text(encoding="utf-8", errors="replace"))
                invoices.extend(inv)
                handled.append(lp.name)
                print(f"  810 {lp.name}: {len(inv)} invoice(s) parsed (not yet applied)")
            s = edi_summarize(invoices)
            print(f"  810 batch: {s['invoices']} invoice(s) ({s['credits']} credit), "
                  f"net ${s['net_total']:,.2f}, net {s['net_cases']:g} case(s)")
            for label, key in (("did NOT reconcile", "unreconciled"),
                               ("no case count", "lines_without_cases"),
                               ("pack size ASSUMED -- cases may be high",
                                "lines_with_estimated_pack"),
                               ("CTT mismatch", "line_count_mismatch"),
                               ("ISS mismatch", "unit_count_mismatch")):
                if s[key]:
                    print(f"      ! {label}: {', '.join(str(x) for x in s[key][:6])}")

        # --- file handled files away so the drop doesn't grow without bound ---
        if handled and processed_dir and not args.dry_run:
            for name in handled:
                conn.file_away(remote_dir, name, processed_dir)
            print(f"  filed {len(handled)} handled file(s) into {processed_dir}/")
        elif handled and not processed_dir:
            print("  NOTE: no processed dir configured -- files stay in the drop "
                  "and will be re-read on every run.")

        if guides and not onhand:
            print("\nWARNING: Cheney's drop contained ONLY order-guide files -- no "
                  "on-hand snapshot arrived, so no warehouse counts were updated. "
                  "This is the open item with Cheney (see "
                  "RUNBOOK_cheney_data_feeds.md).")
        if args.dry_run:
            print("\nDRY RUN -- CSV feed parsed but not applied, drop left "
                  "untouched. Re-run with --commit to apply.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
