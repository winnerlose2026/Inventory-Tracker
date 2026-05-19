#!/usr/bin/env python3
"""Renew the Microsoft Graph change-notification subscriptions.

Designed to run as a Render Cron Job once a day. The Daily Production
webhook only works while the subscription is live; mail subscriptions cap
at ~71 hours, so we refresh every 24h to leave plenty of headroom.

POSTs to /api/graph/subscriptions/renew on the live web service. Prints
the JSON response so it shows up in Render's cron logs.
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
    endpoint = f"{app_url}/api/graph/subscriptions/renew"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Inventory-Token"] = token

    req = urllib.request.Request(
        endpoint, data=b"{}", headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
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
        for r in (parsed.get("results") or []):
            if not r.get("ok"):
                print(f"WARNING: {r.get('user')} renew failed: "
                      f"{r.get('error')}", file=sys.stderr)
    except ValueError:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
