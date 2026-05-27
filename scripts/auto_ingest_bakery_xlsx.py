#!/usr/bin/env python3
"""Watch the Bakery Model spreadsheet for changes and re-ingest when it
has been touched.

Designed to be invoked on a short cadence (every few minutes) by a
scheduled task. It's a no-op when the file hasn't changed since the
last successful run, so it's cheap to run aggressively. The
underlying ingest is idempotent against ``source="bakery-xlsx"`` --
each run replaces the prior set of rows tagged that way -- so even if
this runs twice for the same file state it stays safe.

Mtime tracking
--------------
The last successfully-ingested mtime is written to a sidecar file
next to the workbook (default: ``<xlsx>.last-ingested-mtime``). On
each run we compare ``os.path.getmtime(xlsx)`` against the stamp.
Only when they differ do we shell out to ``ingest_bakery_xlsx.py``.

Mid-sync guard
--------------
Dropbox occasionally exposes the workbook before it's done syncing
the tail of the file. We probe the ZIP central directory before
handing off to ``openpyxl``; if it isn't readable yet, we wait a few
seconds and then defer to the next cron tick instead of surfacing a
``BadZipFile`` stack trace. The stamp is NOT bumped on deferral, so
the next run will retry.

Usage
-----
    INVENTORY_API_TOKEN=... python3 scripts/auto_ingest_bakery_xlsx.py \\
        [--xlsx "../Bakery Model - Sales v. Labor.xlsx"] \\
        [--api-base https://bagel-inventory.onrender.com] \\
        [--stamp <path>] \\
        [--force]

Exit codes
----------
    0  success (either ran a fresh ingest, skipped because unchanged,
       or deferred because the workbook is mid-sync)
    2  xlsx not found / config error
    3  ingest subprocess failed (output is forwarded to stderr)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path


DEFAULT_XLSX = Path(__file__).resolve().parents[2] / "Bakery Model - Sales v. Labor.xlsx"
DEFAULT_API = os.environ.get(
    "INVENTORY_API_BASE",
    "https://bagel-inventory.onrender.com",
)

# When the workbook appears truncated (Dropbox still streaming it down),
# wait up to this many seconds in-process before giving up and deferring
# to the next scheduled run.
SYNC_WAIT_TOTAL_SEC = 12
SYNC_WAIT_INTERVAL_SEC = 3


def _read_stamp(stamp_path: Path):
    if not stamp_path.exists():
        return None
    try:
        return float(stamp_path.read_text().strip())
    except (ValueError, OSError):
        return None


def _write_stamp(stamp_path: Path, mtime: float) -> None:
    stamp_path.write_text(f"{mtime:.6f}\n")


def _workbook_is_complete(path: Path) -> bool:
    """Return True if the file on disk is a fully-written xlsx.

    Dropbox occasionally exposes a partially-synced workbook -- the PK
    header is there but the End-Of-Central-Directory record at the tail
    isn't yet, so ``openpyxl`` blows up with ``BadZipFile``. We probe the
    archive directly so we can defer cleanly instead of surfacing a
    stack trace.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            # ``testzip`` forces a full central-directory walk, which is
            # exactly what fails when the EOCD record hasn't arrived yet.
            zf.testzip()
        return True
    except (zipfile.BadZipFile, OSError):
        return False


def _wait_for_complete_workbook(path: Path, *, quiet: bool) -> bool:
    """Poll the workbook briefly; return True if it became readable."""
    deadline = time.monotonic() + SYNC_WAIT_TOTAL_SEC
    attempts = 0
    while True:
        attempts += 1
        if _workbook_is_complete(path):
            return True
        if time.monotonic() >= deadline:
            if not quiet:
                print(
                    f"workbook still incomplete after {attempts} attempt(s) "
                    f"(~{SYNC_WAIT_TOTAL_SEC}s) -- deferring to next run",
                    file=sys.stderr,
                )
            return False
        time.sleep(SYNC_WAIT_INTERVAL_SEC)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX,
                   help="Path to the workbook (default: %(default)s)")
    p.add_argument("--stamp", type=Path, default=None,
                   help="Path to the mtime sidecar (default: <xlsx>.last-ingested-mtime)")
    p.add_argument("--api-base", default=DEFAULT_API,
                   help="Inventory tracker API base (default: %(default)s)")
    p.add_argument("--token", default=os.environ.get("INVENTORY_API_TOKEN"),
                   help="API token (or set INVENTORY_API_TOKEN)")
    p.add_argument("--force", action="store_true",
                   help="Re-ingest even if the file hasn't changed")
    p.add_argument("--quiet", action="store_true",
                   help="Only print on action / errors -- nothing on no-op")
    args = p.parse_args()

    xlsx = args.xlsx.resolve()
    stamp = (args.stamp or xlsx.with_suffix(xlsx.suffix + ".last-ingested-mtime")).resolve()

    if not xlsx.exists():
        print(f"ERROR: workbook not found at {xlsx}", file=sys.stderr)
        return 2
    if not args.token:
        print("ERROR: --token or INVENTORY_API_TOKEN required", file=sys.stderr)
        return 2

    current_mtime = os.path.getmtime(xlsx)
    last_mtime    = _read_stamp(stamp)

    if not args.force and last_mtime is not None and abs(current_mtime - last_mtime) < 1e-3:
        if not args.quiet:
            print(f"no change since last ingest (mtime={current_mtime:.0f}) -- skipping")
        return 0

    # Locate the sibling ingest script.
    ingest = Path(__file__).resolve().parent / "ingest_bakery_xlsx.py"
    if not ingest.exists():
        print(f"ERROR: ingest script missing at {ingest}", file=sys.stderr)
        return 2

    # Guard against mid-sync Dropbox snapshots. If the workbook isn't a
    # readable zip yet, briefly wait it out and then bail clean -- the
    # next cron tick will pick it up once Dropbox finishes writing.
    if not _wait_for_complete_workbook(xlsx, quiet=args.quiet):
        if not args.quiet:
            print(
                f"workbook at {xlsx} looks mid-sync (no ZIP EOCD); skipping",
                file=sys.stderr,
            )
        # Deliberately do NOT bump the stamp -- we want to retry next run.
        return 0

    print(f"file changed (mtime: {last_mtime} -> {current_mtime:.0f}) -- running ingest")
    env = dict(os.environ)
    env["INVENTORY_API_TOKEN"] = args.token
    proc = subprocess.run(
        [sys.executable, str(ingest), "--xlsx", str(xlsx),
         "--api-base", args.api_base],
        env=env,
    )
    if proc.returncode != 0:
        print(f"ingest exited {proc.returncode}", file=sys.stderr)
        return 3

    _write_stamp(stamp, current_mtime)
    return 0


if __name__ == "__main__":
    sys.exit(main())
