#!/usr/bin/env python3
"""Zero-touch weekly Cheney on-hand ingest.

Pulls Michael Ross's embedded on-hand STOCK images from the app's
/api/email/cheney-stock-images endpoint (server-side Microsoft Graph, so no raw
email attachments are needed client-side), OCRs them with RapidOCR via
cheney_stock_ocr.events_from_image (item#-authoritative mapping), and POSTs the
resulting on_hand events to /api/email/ingest-events.

VERIFY-BEFORE-COMMIT: dry-run by default (prints the extracted table); --commit
applies. A facility is skipped (never committed) if OCR produced any warning
(unresolved row, under-read image, missing date, duplicate variety) or a token
below --min-score. Idempotent: re-running is a safe no-op ("unchanged").

Creds: --app-url / --api-token or APP_URL / INVENTORY_API_TOKEN env.
"""
import argparse
import base64
import json
import os
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                      # scripts/
sys.path.insert(0, os.path.dirname(_HERE))     # repo root (integrations)
import cheney_stock_ocr as ocr  # noqa: E402


def _get(base, token, path, timeout=90):
    req = urllib.request.Request(base.rstrip("/") + path, method="GET",
                                 headers={"X-Inventory-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(base, token, payload, timeout=90):
    req = urllib.request.Request(base.rstrip("/") + "/api/email/ingest-events",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Inventory-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--commit", action="store_true",
                   help="Apply (default: dry-run + verification table only)")
    p.add_argument("--lookback-days", type=int, default=9)
    p.add_argument("--min-score", type=float, default=0.5)
    p.add_argument("--app-url", default=os.environ.get("APP_URL", "https://bagel-inventory.onrender.com"))
    p.add_argument("--api-token", default=os.environ.get("INVENTORY_API_TOKEN", ""))
    a = p.parse_args(argv)
    if not a.api_token:
        print("No API token (set INVENTORY_API_TOKEN or --api-token).", file=sys.stderr)
        return 2

    try:
        data = _get(a.app_url, a.api_token,
                    f"/api/email/cheney-stock-images?lookback_days={a.lookback_days}")
    except Exception as exc:
        print(f"endpoint GET failed: {exc}", file=sys.stderr)
        return 2
    if not data.get("ok"):
        print(f"endpoint error: {data.get('error')}", file=sys.stderr)
        return 2
    facilities = data.get("facilities", [])
    for e in (data.get("errors") or []):
        print("  endpoint note:", e)
    if not facilities:
        print("No Cheney stock images found in the lookback window "
              f"({a.lookback_days}d) -- nothing to ingest.")
        return 0

    commit_events, blocked = [], False
    for f in facilities:
        wh, cd = f["warehouse"], f.get("count_date", "")
        try:
            img = base64.b64decode(f["image_b64"])
        except Exception as exc:
            print(f"\n=== {wh} ===\n   ERROR decoding image: {exc}"); blocked = True; continue
        events, warnings, notes = ocr.events_from_image(img, wh, cd, source="cheney-stock-endpoint")
        low = [e for e in events if e.get("_min_score", 1.0) < a.min_score]
        print(f"\n=== {wh}  (count_date={cd or '?'}, {len(events)} rows) ===")
        for e in sorted(events, key=lambda z: z["item"]["variety"]):
            print(f"   {e['item']['variety']:24} {int(e['item']['quantity']):>5}  "
                  f"sku={e['item'].get('distributor_sku','')}  score={e.get('_min_score',0):.2f}")
        for n in notes:
            print("   note:", n)
        for w in warnings:
            print("   WARN:", w)
        if warnings or low or not events:
            print(f"   -> BLOCKED from commit (warnings={len(warnings)}, low_score={len(low)})")
            blocked = True
        else:
            commit_events += events

    if not a.commit:
        print(f"\nDRY-RUN. {len(commit_events)} events ready across "
              f"{len({e['item']['warehouse'] for e in commit_events})} clean facility(ies). "
              f"Re-run with --commit to apply.")
        return 0
    if not commit_events:
        print("\nNothing clean to commit.", file=sys.stderr)
        return 2 if blocked else 0
    payload = {"dry_run": False, "source": "cheney-stock-ocr/endpoint",
               "messages_seen": len(facilities), "messages_parsed": len(facilities),
               "events": [{k: v for k, v in e.items() if not k.startswith("_")} for e in commit_events]}
    resp = _post(a.app_url, a.api_token, payload)
    rep = (resp.get("reports") or [{}])[0]
    print(f"\nCOMMITTED: {rep.get('by_event_type')} updated={rep.get('updated')} "
          f"unchanged={rep.get('unchanged')}")
    if blocked:
        print("NOTE: one or more facilities were blocked and NOT committed -- review above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
