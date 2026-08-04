#!/usr/bin/env python3
"""Ingest a Cheney Brothers daily on-hand inventory CSV (the SFTP feed).

Reads a CSV drop (agreed with Walt Wilcox / Cheney 2026-07-06), parses it with
integrations.cheney_csv_inventory, and POSTs the resulting on_hand events to
the tracker's /api/email/ingest-events (same apply pipeline as the mailbox
scan). on_hand events are latest-wins per (variety, warehouse), so re-running
the same drop is idempotent.

Intended to run on a schedule once the SFTP drop is live: pull today's CSV,
then:

    INVENTORY_API_TOKEN=... python scripts/ingest_cheney_inventory_csv.py \
        --csv /path/to/todays_cheney_inventory.csv --commit

Dry-run (default) prints what would be applied without posting.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from integrations.cheney_csv_inventory import parse_inventory_csv  # noqa: E402

DEFAULT_API = (os.environ.get("APP_URL") or os.environ.get("INVENTORY_API_BASE")
               or "https://bagel-inventory.onrender.com").rstrip("/")
SOURCE = "cheney-sftp-csv"


def _post(url: str, body: dict, token):
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Inventory-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--csv", required=True, help="Path to the Cheney daily on-hand CSV")
    p.add_argument("--api-base", default=DEFAULT_API, help="Tracker base URL")
    p.add_argument("--token", default=os.environ.get("INVENTORY_API_TOKEN"),
                   help="API token (or set INVENTORY_API_TOKEN) -- sent as X-Inventory-Token")
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="Parse + print only (default)")
    p.add_argument("--commit", dest="dry_run", action="store_false",
                   help="Actually POST the events to the tracker")
    p.add_argument("--allow-all-zero", action="store_true",
                   help="Accept a CSV whose on-hand column is zero on every "
                        "row. Refused by default, because applying it zeroes "
                        "out every warehouse's count -- pass this ONLY for a "
                        "deliberate zero-out.")
    args = p.parse_args()

    path = Path(args.csv)
    if not path.exists():
        print(f"ERROR: CSV not found: {path}", file=sys.stderr)
        return 2
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    events, errors = parse_inventory_csv(text, filename=path.name,
                                        allow_all_zero=args.allow_all_zero)

    print(f"Parsed {len(events)} on_hand event(s), {len(errors)} issue(s) from {path.name}")
    for e in errors:
        print("  ISSUE:", e)
    wh = sorted({ev["item"]["warehouse"] for ev in events})
    print("  Warehouses:", ", ".join(wh) or "(none)")

    if args.dry_run:
        print("\nDRY RUN -- not posting. Re-run with --commit to apply.")
        return 0
    if not args.token:
        print("ERROR: --token or INVENTORY_API_TOKEN required for --commit", file=sys.stderr)
        return 2
    if not events:
        print("Nothing to post.")
        return 0

    body = {
        "dry_run": False,
        "source": SOURCE,
        "messages_seen": 1,
        "messages_parsed": 1 if events else 0,
        "errors": errors,
        "events": events,
    }
    status, resp = _post(f"{args.api_base}/api/email/ingest-events", body, args.token)
    print(f"POST /api/email/ingest-events -> HTTP {status}")
    print(resp[:800])
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
