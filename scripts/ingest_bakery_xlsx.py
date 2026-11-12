#!/usr/bin/env python3
"""Ingest the "Bakery Model - Sales v. Labor" workbook into the inventory
tracker.

Pulled here because the production bakery isn't wired to Toast yet — JD
updates this spreadsheet weekly and reuploads it. Each sheet is one ISO
week (Mon-Sun) named like ``5.11.26 - 5.17.26`` (or earlier sheets like
``7.19 - 7.25`` with no year). Sheet ordering is the source of truth for
which week a sheet represents — we walk 7 days at a time from
2021-07-19 (sheet 0) and cross-check against any explicit year stamped
in the sheet name.

What we read from each sheet
----------------------------
    B3..H3    daily sales (Mon..Sun)            -- weekly_total / 7
    I3        weekly total sales
    B10..H10  daily labor $ (Mon..Sun)          -- REAL daily values
    B11..H11  daily labor hours (Mon..Sun)      -- REAL daily values
    I10       weekly total labor $
    I11       weekly total labor hours
    I13       Labor $ - Gabo & Taxes (alternate metric -- not stored)
    N3..N8    selling channel names
    O3..O8    selling channel sale $
    O10       SPLH (sales per labor hour)

What we push
------------
    POST /api/admin/labor/ingest          (daily labor entries, source=bakery-xlsx)
    POST /api/admin/bakery-sales/ingest   (weekly sales rows,    source=bakery-xlsx)

Both calls use ``replace=True`` so every run is fully idempotent against
its source tag — old bakery-xlsx rows are wiped and replaced with the
fresh parse.

Usage
-----
    INVENTORY_API_TOKEN=... python scripts/ingest_bakery_xlsx.py \
        --xlsx "../Bakery Model - Sales v. Labor.xlsx" \
        [--api-base https://bagel-inventory.onrender.com] \
        [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# The parser lives in integrations/bakery_workbook.py so the server's
# POST /api/admin/bakery-workbook/upload and this laptop script read the
# workbook the same way. It is loaded BY FILE, not as `integrations.…`: the
# package's __init__ imports the mail/PDF stack, which the laptop that runs
# this script does not need installed.
import importlib.util  # noqa: E402

_BW_PATH = Path(__file__).resolve().parent.parent / "integrations" / "bakery_workbook.py"
_spec = importlib.util.spec_from_file_location("hh_bakery_workbook", _BW_PATH)
_bw = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("hh_bakery_workbook", _bw)
_spec.loader.exec_module(_bw)

CHANNEL_ROWS, DOW_COLS = _bw.CHANNEL_ROWS, _bw.DOW_COLS
SOURCE_TAG, WORKBOOK_START = _bw.SOURCE_TAG, _bw.WORKBOOK_START
WorkbookError = _bw.WorkbookError
parse_sheet, parse_workbook = _bw.parse_sheet, _bw.parse_workbook
parse_workbook_detailed, resolve_weeks = _bw.parse_workbook_detailed, _bw.resolve_weeks
_num, _all_equal = _bw._num, _bw._all_equal
_parse_sheet_name, _walk_week = _bw._parse_sheet_name, _bw._walk_week

DEFAULT_API = os.environ.get(
    "INVENTORY_API_BASE",
    "https://bagel-inventory.onrender.com",
)
DEFAULT_XLSX = "Bakery Model - Sales v. Labor.xlsx"


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def _post(url: str, body: dict, token):
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Inventory-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")
        raise SystemExit("POST " + url + " -> " + str(e.code) + ": " + msg)


def push(api_base: str, labor, weeks, token):
    if labor:
        out = _post(api_base.rstrip("/") + "/api/admin/labor/ingest",
                    {"entries": labor, "replace": True}, token)
        print("labor:        " + json.dumps(out))
    if weeks:
        out = _post(api_base.rstrip("/") + "/api/admin/bakery-sales/ingest",
                    {"entries": weeks, "replace": True}, token)
        print("bakery-sales: " + json.dumps(out))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--xlsx", default=DEFAULT_XLSX,
                   help="Path to the workbook (default: %(default)s)")
    p.add_argument("--api-base", default=DEFAULT_API,
                   help="Base URL of the inventory tracker API")
    p.add_argument("--token", default=os.environ.get("INVENTORY_API_TOKEN"),
                   help="API token (or set INVENTORY_API_TOKEN) -- sent as X-Inventory-Token")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse and print a summary; don't POST")
    p.add_argument("--force", action="store_true",
                   help="Ingest even when sheet names don't line up into clean "
                        "Monday weeks (best-effort dates are used)")
    args = p.parse_args()

    path = Path(args.xlsx)
    if not path.exists():
        print("ERROR: workbook not found at " + str(path), file=sys.stderr)
        return 2

    try:
        res = parse_workbook_detailed(path, force=args.force)
    except WorkbookError as exc:
        # Nothing is posted: every run REPLACES all stored bakery rows, so a
        # mis-dated or truncated workbook must not overwrite good history.
        print("ERROR: " + str(exc), file=sys.stderr)
        for prob in exc.problems:
            print("  - " + prob, file=sys.stderr)
        return 5
    labor, weeks = res["labor"], res["weeks"]
    for n in res["problems"]:
        print("WARNING (forced): " + n, file=sys.stderr)
    for n in res["notes"]:
        print("note: " + n, file=sys.stderr)
    print("Parsed " + str(len(weeks)) + " non-empty week(s), " +
          str(len(labor)) + " daily labor entries.")
    if weeks:
        first, last = weeks[0]["week_start"], weeks[-1]["week_start"]
        print("Week range: " + first + " .. " + last)
        for w in weeks[-3:]:
            ch = ", ".join(k + "=$" + format(v, ",.0f")
                           for k, v in w["channels"].items())
            print("  " + w["week_start"] +
                  "  total=$" + format(w["total"], ",.2f") +
                  "  [" + ch + "]")

    if args.dry_run:
        print("(dry-run -- no POST)")
        return 0

    if not args.token:
        print("ERROR: --token or INVENTORY_API_TOKEN required for POST",
              file=sys.stderr)
        return 2

    push(args.api_base, labor, weeks, args.token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
