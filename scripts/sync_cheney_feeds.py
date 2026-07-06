#!/usr/bin/env python3
"""Daily sync of Cheney Brothers' SFTP feeds into the tracker.

The receiving side lives on Render (POST /api/ingest/cheney-inventory-csv);
this is the *bridge* that pulls Cheney's daily SFTP drop and hands it over.
Runs equally well as a Render Cron Job or a scheduled Cowork routine.

Feeds (agreed with Walt Wilcox / Cheney, 2026-07-06):
  * Daily on-hand inventory CSV  -> POSTed to the tracker (applied as on_hand)
  * Daily EDI 810 invoices        -> parsed + summarized (downstream wiring is
                                     deferred until a real 810 confirms field
                                     semantics + money-decimal convention)

CONFIG (all via env; the script no-ops quietly until SFTP is configured):
  CHENEY_SFTP_HOST        sftp hostname                    (required to run)
  CHENEY_SFTP_PORT        default 22
  CHENEY_SFTP_USER        username                         (required to run)
  CHENEY_SFTP_KEY         path to a private key file  (or)
  CHENEY_SFTP_PASSWORD    password
  CHENEY_SFTP_DIR         remote directory to scan         (default ".")
  CHENEY_SFTP_CSV_GLOB    default "*.csv"
  CHENEY_SFTP_810_GLOB    default "*.edi,*.810,*.x12,*.txt"
  INVENTORY_API_TOKEN     tracker API token (X-Inventory-Token)
  APP_URL / INVENTORY_API_BASE   tracker base URL

Usage:
  python scripts/sync_cheney_feeds.py            # dry run (default): pull + report
  python scripts/sync_cheney_feeds.py --commit   # actually apply the CSV feed

Requires paramiko for the SFTP pull (pip install paramiko); imported lazily so
the rest of the repo/tests don't depend on it.
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


def _sftp_configured() -> bool:
    return bool(os.environ.get("CHENEY_SFTP_HOST") and os.environ.get("CHENEY_SFTP_USER"))


def _connect():
    try:
        import paramiko  # lazy: only needed when actually pulling
    except ImportError:
        print("ERROR: paramiko not installed (pip install paramiko).", file=sys.stderr)
        raise SystemExit(3)
    host = os.environ["CHENEY_SFTP_HOST"]
    port = int(os.environ.get("CHENEY_SFTP_PORT") or 22)
    user = os.environ["CHENEY_SFTP_USER"]
    key = os.environ.get("CHENEY_SFTP_KEY")
    pw = os.environ.get("CHENEY_SFTP_PASSWORD")
    t = __import__("paramiko").Transport((host, port))
    if key:
        pkey = paramiko.RSAKey.from_private_key_file(key)
        t.connect(username=user, pkey=pkey)
    else:
        t.connect(username=user, password=pw)
    return paramiko.SFTPClient.from_transport(t), t


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

    if not _sftp_configured():
        print("Cheney SFTP not configured yet (set CHENEY_SFTP_HOST/USER/...). "
              "Nothing to do -- exiting cleanly.")
        return 0

    csv_globs = (os.environ.get("CHENEY_SFTP_CSV_GLOB") or "*.csv").split(",")
    edi_globs = (os.environ.get("CHENEY_SFTP_810_GLOB") or "*.edi,*.810,*.x12,*.txt").split(",")
    remote_dir = os.environ.get("CHENEY_SFTP_DIR") or "."

    sftp, transport = _connect()
    tmp = Path(tempfile.mkdtemp(prefix="cheney_sftp_"))
    csv_files, edi_files = [], []
    try:
        for name in sftp.listdir(remote_dir):
            rp = posixpath.join(remote_dir, name)
            if _matches(name, csv_globs):
                lp = tmp / name
                sftp.get(rp, str(lp))
                csv_files.append(lp)
            elif _matches(name, edi_globs):
                lp = tmp / name
                sftp.get(rp, str(lp))
                edi_files.append(lp)
    finally:
        sftp.close(); transport.close()

    print(f"Pulled {len(csv_files)} CSV + {len(edi_files)} EDI file(s) from {remote_dir}")

    # --- CSV on-hand -> tracker ---
    dry = "1" if args.dry_run else "0"
    for lp in sorted(csv_files):
        url = f"{args.api_base}/api/ingest/cheney-inventory-csv?dry_run={dry}&filename={lp.name}"
        status, resp = _post(url, lp.read_bytes(), args.token, "text/csv")
        print(f"  CSV {lp.name}: POST -> HTTP {status}  {resp[:200]}")

    # --- EDI 810 -> parse + summarize only (routing deferred) ---
    if edi_files:
        from integrations.edi_810 import parse_810
        for lp in sorted(edi_files):
            inv = parse_810(lp.read_text(encoding="utf-8", errors="replace"))
            print(f"  810 {lp.name}: {len(inv)} invoice(s) parsed (not yet applied)")

    if args.dry_run:
        print("\nDRY RUN -- CSV feed parsed but not applied. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
