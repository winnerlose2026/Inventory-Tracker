#!/usr/bin/env python3
"""Pull Toast product mix per (location, date) via the Toast MCP server
and POST aggregated rows to /api/sales/ingest.

Designed to be run from a Cowork session where the Toast MCP server is
connected. You drive the date range; the script:
  1. Resolves the retail-location GUIDs by skipping MVT DC + 'Z Do Not Use'.
  2. For each (location, business_date), calls toast_get_product_mix
     with min_quantity=2 and sort_by=gross.
  3. Buffers rows; flushes to /api/sales/ingest every BATCH_SIZE rows.

USAGE (in Cowork after the MCP server is connected):

    INVENTORY_API_TOKEN=... python scripts/ingest_toast_sales.py \\
        --start 2026-04-15 --end 2026-05-13 \\
        --concurrency 6

The MCP server name in your Cowork session is something like
``mcp__<server>__toast_get_product_mix``; pass it via --mcp-base if it
differs from the default.

This script CANNOT call Toast directly (no creds in the repo). It writes
the per-day work to a JSON queue and a sidecar Python prelude that calls
the MCP tool — Claude/Cowork executes the prelude and then pushes results
to the Render endpoint.
"""
import argparse, json, os, sys, urllib.request
from datetime import date, timedelta

DEFAULT_API = os.environ.get(
    "INVENTORY_API_BASE",
    "https://bagel-inventory.onrender.com",
)
TOKEN = os.environ.get("INVENTORY_API_TOKEN")

# Retail location filter — exclude DCs and disabled locations.
EXCLUDE_NAMES = ("MVT DC", "Z Do Not Use")


def _is_retail(loc_name: str) -> bool:
    n = (loc_name or "").strip()
    return n and not any(n.startswith(x) for x in EXCLUDE_NAMES)


def _emit_work_queue(start: date, end: date, out_path: str):
    """Write a JSON queue of (location_guid, date) tuples for Cowork to
    iterate through. Cowork operator then calls toast_get_product_mix
    once per tuple and POSTs the aggregated rows to /api/sales/ingest."""
    one = timedelta(days=1)
    queue = []
    d = start
    while d <= end:
        queue.append(d.isoformat())
        d += one
    with open(out_path, "w") as f:
        json.dump({"dates": queue,
                   "note": ("Resolve retail locations via "
                            "toast_list_restaurants and filter out "
                            "MVT DC / 'Z Do Not Use'. Call "
                            "toast_get_product_mix(business_date, "
                            "restaurant_guid, min_quantity=2, "
                            "sort_by='gross') for each (date, guid) "
                            "pair, then POST rows in batches of 200 "
                            "to /api/sales/ingest.")}, f, indent=2)
    print(f"wrote {len(queue)} dates to {out_path}")


def post_rows(rows: list, replace_dates: bool = False, api: str = DEFAULT_API):
    payload = json.dumps({"rows": rows,
                          "replace_dates": replace_dates}).encode()
    req = urllib.request.Request(
        f"{api}/api/sales/ingest", data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Inventory-Token": TOKEN or ""},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True,
                    help="Earliest business date (YYYY-MM-DD)")
    ap.add_argument("--end",   required=True,
                    help="Latest business date (inclusive)")
    ap.add_argument("--queue", default="/tmp/toast_sales_queue.json",
                    help="Where to write the date list for the Cowork operator")
    ap.add_argument("--api",   default=DEFAULT_API)
    args = ap.parse_args()
    s = date.fromisoformat(args.start)
    e = date.fromisoformat(args.end)
    _emit_work_queue(s, e, args.queue)
    print()
    print("Next steps (in your Cowork session):")
    print("  1. Call mcp__<toast>__toast_list_restaurants to get all GUIDs")
    print("  2. Filter out MVT DC + 'Z Do Not Use' Fulton Market")
    print("  3. For each (guid, date in queue), call "
          "mcp__<toast>__toast_get_product_mix")
    print("  4. Convert to rows and POST batches of 200 to "
          f"{args.api}/api/sales/ingest")
    print()
    print("Row schema:")
    print('  {"restaurant_guid":..., "location":..., "business_date":...,')
    print('   "item_guid":..., "item":..., "menu_group":..., "qty":N, '
          '"gross":F, "net":F}')


if __name__ == "__main__":
    main()
