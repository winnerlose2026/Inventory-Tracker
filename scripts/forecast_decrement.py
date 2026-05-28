#!/usr/bin/env python3
"""Daily forecast-decrement cron entrypoint.

Calls POST /api/forecast/decrement-daily on the running web service. The
endpoint walks every SKU with weekly_usage > 0 and posts a positive usage
event of weekly_usage/7 (idempotent per UTC date), driving FIFO lot
consumption forward without anyone having to record usage manually.

The weekly true-up against vendor on-hand snapshots reverses these
forecast entries when ground-truth data comes in.

Environment:
    APP_URL                  base URL of the web service (e.g.
                             https://bagel-inventory.onrender.com)
    INVENTORY_API_TOKEN      same token as used by the other crons

Exit codes:
    0 — success (any HTTP 2xx, including 0 SKUs applied)
    1 — bad config, network failure, or non-2xx response
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    base = (os.environ.get("APP_URL") or "").rstrip("/")
    token = os.environ.get("INVENTORY_API_TOKEN") or ""
    if not base or not token:
        print("ERROR: APP_URL and INVENTORY_API_TOKEN must be set", file=sys.stderr)
        return 1
    url = f"{base}/api/forecast/decrement-daily"
    req = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Inventory-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"ERROR: HTTP {exc.code} from {url}\n{body}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    applied = payload.get("applied") or []
    skipped = payload.get("skipped_existing") or []
    print(
        f"forecast-decrement {payload.get('date','?')}: "
        f"{len(applied)} applied, {len(skipped)} already-done"
    )
    if applied[:10]:
        for a in applied[:10]:
            print(f"  -{a['daily_amount']:.4f} cs  {a['name']}  ({a['old_quantity']} -> {a['new_quantity']})")
        if len(applied) > 10:
            print(f"  ... and {len(applied) - 10} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
