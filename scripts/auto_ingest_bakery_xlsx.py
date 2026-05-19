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

Usage
-----
    INVENTORY_API_TOKEN=... python3 scripts/auto_ingest_bakery_xlsx.py \\
        [--xlsx "../Bakery Model - Sales v. Labor.xlsx"] \\
        [--api-base https://bagel-inventory.onrender.com] \\
        [--stamp <path>] \\
        [--force]

Exit codes
----------
    0  success (either ran a fresh ingest, or skipped because unchanged)
    2  xlsx not found / config error
    3  ingest subprocess failed (output is forwarded to stderr)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_XLSX = Path(__file__).resolve().parents[2] / "Bakery Model - Sales v. Labor.xlsx"
DEFAULT_API = os.environ.get(
    "INVENTORY_API_BASE",
    "https://bagel-inventory.onrender.com",
)


def _read_stamp(stamp_path: Path):
    if not stamp_path.exists():
        return None
    try:
        return float(stamp_path.read_text().strip())
    except (ValueError, OSError):
        return None


def _write_stamp(stamp_path: Path, mtime: float) -> None:
    stamp_path.write_text(f"{mtime:.6f}\n")


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
