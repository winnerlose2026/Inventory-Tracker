#!/usr/bin/env python3
"""Fire POST /api/email/scan on the live web service.

Designed to run as a Render Cron Job. Reads APP_URL and
INVENTORY_API_TOKEN from the environment and prints the JSON report
(or the error) so it shows up in Render's cron logs.
"""

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    app_url = os.environ.get("APP_URL", "").rstrip("/")
    if not app_url:
        print("ERROR: APP_URL is not set. Point it at the web service, "
              "e.g. https://bagel-inventory.onrender.com", file=sys.stderr)
        return 2

    token = os.environ.get("INVENTORY_API_TOKEN", "").strip()
    endpoint = f"{app_url}/api/email/scan"
    # Ask the web service for a wider sweep than the defaults: 14-day
    # lookback with a per-mailbox budget of 200. The scan bounds the sweep
    # by date only (NOT hasAttachments) and pre-qualifies each message by
    # sender/recipient before downloading MIME, so body-pasted reports
    # (e.g. the US Foods Zebulon weekly inventory report) are caught too.
    # The default (max_messages=60, no lookback) only walks the 60
    # most-recent messages -- which biases the scan against slow-arrival
    # senders (Chefs Warehouse drops 1-2 POs a week; if other senders pile
    # in 60+ messages between cron runs, CW emails fall off the top of the
    # list and never get parsed). 14 days fits comfortably in the 180s
    # gunicorn budget, the PO revision-replace logic in _apply_events makes
    # re-scanning the same message idempotent, and CW POs land via the
    # parallel cw_pos channel.
    body = json.dumps({
        "dry_run": False,
        "lookback_days": 14,
        "max_messages": 200,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Inventory-Token"] = token

    req = urllib.request.Request(endpoint, data=body, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTPError {exc.code} {exc.reason} from {endpoint}",
              file=sys.stderr)
        print(body, file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"URLError reaching {endpoint}: {exc.reason}", file=sys.stderr)
        return 1

    print(f"HTTP {status} from {endpoint}")
    try:
        parsed = json.loads(payload)
        print(json.dumps(parsed, indent=2))
        for report in (parsed.get("reports") or []):
            if report.get("error"):
                print(f"WARNING: {report['distributor']} reported error: "
                      f"{report['error']}", file=sys.stderr)
    except ValueError:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
