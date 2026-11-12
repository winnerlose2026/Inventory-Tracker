#!/usr/bin/env python3
"""Lineage PO follow-ups -- the `lineage-po-followups` Render cron.

Until 2026-11 that cron ran `python3 -c "$FOLLOWUP_SCRIPT"` from an env var:
the code that decides whether Lineage gets chased lived nowhere in the repo.
This is that job, from the API contract in blueprints/delivery.py:

    1. POST /api/pos/delivery/sync-threads    re-read the Quote and Schedule
                                              threads (the server applies the
                                              plain confirmations it finds)
    2. POST /api/pos/delivery/send-followups  the SERVER decides who is due,
                                              writes and sends the emails

Environment:
    APP_URL               the web service, e.g. https://bagel-inventory.onrender.com
    DELIVERY_API_TOKEN    the limited delivery token (falls back to
                          INVENTORY_API_TOKEN); sent as X-Inventory-Token
    FOLLOWUP_DRY_RUN=1    (or --dry-run) report what would be sent, send nothing
    FOLLOWUP_LOOKBACK_DAYS  sync lookback, default 60

Exit status (Render marks the run failed on non-zero, which is the alert):
    0  both calls succeeded and nothing needs a person
    1  an HTTP error / timeout (after 2 retries on 502/503/504/timeout), a
       response with ok:false, a follow-up whose send is unconfirmed
       (`unconfirmed_sends`), a due PO with no thread to reply on
       (`due_without_thread`), or an email that failed to send
    2  not configured (APP_URL / token missing)

Standard library only: the cron has no build step.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

RETRY_STATUS = (502, 503, 504)
RETRIES = 2
RETRY_WAIT_S = 20
TIMEOUT_S = 180


class CallFailed(Exception):
    pass


def _post(opener, url: str, token: str, body: dict, *, sleep=time.sleep,
          timeout: int = TIMEOUT_S) -> dict:
    """POST JSON, retrying 502/503/504 and timeouts twice. Returns the parsed
    JSON body; raises CallFailed on any other HTTP error or a non-JSON reply.
    Re-posting send-followups is safe: the server records the chase before it
    sends, so a retry finds nothing due."""
    data = json.dumps(body).encode("utf-8")
    last = ""
    for attempt in range(RETRIES + 1):
        if attempt:
            sleep(RETRY_WAIT_S * attempt)
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Inventory-Token": token,
                     "User-Agent": "python-lineage-followups"})
        try:
            with opener(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            last = f"HTTP {exc.code} from {url}: {detail}"
            if exc.code in RETRY_STATUS:
                continue
            raise CallFailed(last) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            last = f"{type(exc).__name__} reaching {url}: {getattr(exc, 'reason', exc)}"
            continue
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise CallFailed(f"non-JSON reply from {url}: {raw[:200]!r}") from exc
        if not isinstance(parsed, dict):
            raise CallFailed(f"unexpected reply from {url}: {raw[:200]!r}")
        return parsed
    raise CallFailed(f"{last} (gave up after {RETRIES + 1} attempts)")


def run(env=None, argv=None, opener=None, sleep=time.sleep, out=None) -> int:
    env = os.environ if env is None else env
    argv = sys.argv[1:] if argv is None else argv
    opener = opener or urllib.request.urlopen
    say = out or print
    app_url = (env.get("APP_URL") or "").rstrip("/")
    token = (env.get("DELIVERY_API_TOKEN") or env.get("INVENTORY_API_TOKEN") or "").strip()
    if not app_url or not token:
        say("lineage-followups: NOT CONFIGURED -- APP_URL and DELIVERY_API_TOKEN "
            "(or INVENTORY_API_TOKEN) must be set")
        return 2
    dry_run = "--dry-run" in argv or (env.get("FOLLOWUP_DRY_RUN") or "") in ("1", "true", "yes")
    try:
        lookback = int(env.get("FOLLOWUP_LOOKBACK_DAYS") or 60)
    except ValueError:
        lookback = 60

    problems = []
    try:
        sync = _post(opener, f"{app_url}/api/pos/delivery/sync-threads", token,
                     {"lookback_days": lookback}, sleep=sleep)
    except CallFailed as exc:
        say(f"lineage-followups: FAILED sync-threads -- {exc}")
        return 1
    if not sync.get("ok"):
        say(f"lineage-followups: FAILED sync-threads ok:false -- {sync.get('error')}")
        return 1
    if sync.get("errors"):
        problems.append(f"sync read was partial: {'; '.join(map(str, sync['errors']))[:300]}")

    try:
        sent = _post(opener, f"{app_url}/api/pos/delivery/send-followups", token,
                     {"dry_run": dry_run}, sleep=sleep)
    except CallFailed as exc:
        say(f"lineage-followups: FAILED send-followups -- {exc}")
        return 1
    emails = sent.get("emails") or []
    if not sent.get("ok"):
        problems.append(f"send-followups ok:false: {sent.get('error')}")
    if sent.get("unconfirmed_sends"):
        problems.append("send not confirmed (check JD's Drafts / Sent Items) for thread(s) "
                        + ", ".join(map(str, sent["unconfirmed_sends"])))
    if sent.get("due_without_thread"):
        problems.append("due but no Quote and Schedule thread to reply on: "
                        + ", ".join(map(str, sent["due_without_thread"])))
    for e in emails:
        if e.get("error") and e.get("conversation_id") not in (sent.get("unconfirmed_sends") or []):
            problems.append(f"email for {', '.join(e.get('po_numbers') or []) or '?'} "
                            f"failed: {e['error']}")

    n_sent = int(sent.get("sent") or 0)
    would = sum(1 for e in emails if e.get("would_send"))
    held = sum(len(e.get("held_back") or {}) for e in emails)
    line = (f"lineage-followups: {'DRY RUN ' if dry_run else ''}"
            f"threads={len(sync.get('threads') or [])} "
            f"registered={len(sync.get('registered') or [])} "
            f"confirmed_by_server={sum(1 for c in (sync.get('confirmed_by_server') or []) if c.get('applied'))} "
            f"not_in_tracker={len(sync.get('not_in_tracker') or [])} "
            f"sent={n_sent}" + (f" would_send={would}" if dry_run else "")
            + f" held_back={held} closed={len(sent.get('closed') or [])}")
    if problems:
        say(line + " -- PROBLEMS: " + " | ".join(problems))
        return 1
    say(line + " -- ok")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
