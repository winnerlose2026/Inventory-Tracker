# Handoff: finishing the /api/email/scan 500 fix

## What's done

Three commits pushed to `origin/claude/update-m365-credentials-QvulU`:

1. `65b67df` — wrap the route handler in one big try/except so any exception becomes a structured 200 with `traceback` field (no more bare HTML 500 pages).
2. `9a70586` — pull the scan in-line in the route, default `max_messages=60` (down from 300), accept a body field to override.
3. `e0d5433` — move the lazy `EmailInboxClient` import back inside the try block (an oversight from the prior commit).

## Why it's still 500ing

Pulling the live deploy state confirmed two service-side issues that no commit can fix:

### 1. Auto-deploy is off
```
"autoDeploy": "no"
"autoDeployTrigger": "off"
```
The last successful deploy was `dep-d7ls6vpkh4rs738ivpb0` from commit `1e571c7` (April 24). All three of my hardening commits sit on GitHub un-deployed, which is why probing the endpoint keeps returning the identical pre-fix 500.

### 2. The saved startCommand drops `--timeout 180`
```
"startCommand": "gunicorn app:app"
```
The Procfile in the repo says `gunicorn --workers 2 --timeout 180 --bind 0.0.0.0:$PORT app:app`, but Render's dashboard-saved start command takes precedence over the Procfile. Without `--timeout 180`, gunicorn defaults to **30 seconds**. Render's logs make the failure mode unambiguous:

```
[CRITICAL] WORKER TIMEOUT (pid:51)
File ".../email_scanner.py", line 640, in _scan_ms365_mailbox
    mime_bytes, _ = self._graph_get(mime_url, token, accept="text/plain")
SystemExit: 1
```

`SystemExit` is a `BaseException`, not an `Exception` — `except Exception` can't catch it, which is why the user-side traceback wrapping doesn't help here.

## What you need to do (two clicks)

Open the service settings: <https://dashboard.render.com/web/srv-d7j6l65ckfvc73fk7ua0/settings>

1. **Start command** — set it to:
   ```
   gunicorn --workers 2 --timeout 180 --bind 0.0.0.0:$PORT app:app
   ```
   This restores the 180-second worker timeout the Procfile already specifies.

2. **Auto-deploy** — flip to **On Commit** (or use the **Manual Deploy → Deploy latest commit** button to apply just the current head). The branch is already correct: `claude/update-m365-credentials-QvulU`.

After the next deploy:
- A normal `/api/email/scan` call should return 200 with a real report (or a structured 200 with `status: "error"` + traceback excerpt if the scan still fails for any reason).
- The new default of `max_messages=60` keeps a single call comfortably under 180s. Pass `"max_messages": 200` in the body for a deeper sweep.

## Future-proofing (optional)

The Cowork scheduled task no longer depends on `/api/email/scan` at all — it now runs `scripts/cowork_graph_scan.py` and posts to `/api/email/ingest-events`, which is fast and idempotent. So even if you don't get to the dashboard right away, the 4-hour mailbox scan is unaffected. The `/api/email/scan` endpoint is now an ad-hoc / manual-sweep tool only.
