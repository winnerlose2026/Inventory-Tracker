# Blueprint + JS extraction refactor (#8) — plan

**Status:** in progress on branch `refactor/blueprints-and-js`. First slice
(health blueprint) landed as the proof-of-pattern. Everything else is staged
below. This branch is **never merged directly to `main` without review** — each
slice is its own CI-gated PR.

## Why
`app.py` (~4.8k lines) and `templates/index.html` (~5.7k lines of inline JS/CSS)
are the top maintainability risk. Splitting into blueprints + static JS shrinks
the blast radius of any change and lets CI test pieces in isolation.

## Target architecture
- `app.py` → thin entry: create the Flask app, register blueprints, keep the
  `before_request` auth gate. (Eventually an `create_app()` factory.)
- `core/` → shared, blueprint-agnostic helpers so blueprints never import
  `app.py` (avoids import cycles):
  - `core/auth.py` — `_user_logged_in`, `_has_valid_api_token`, `_is_authenticated`, `_OPEN_ENDPOINTS`, the gate.
  - `core/errors.py` — `_log_exc`, `_safe_err`.
  - `core/http.py` — outbound request helpers / host allowlist.
  - data layer already lives in `inventory_tracker.py` / `sync_inventory.py`.
- `blueprints/` → one module per domain, each a `Blueprint`:
  - `health` ✅ (this slice), `webhooks` (Graph), `inventory`, `pending_pos`,
    `freight`, `production`, `email_scan`, `admin`.
- `static/js/` → inline `<script>` bodies extracted into modules; templates load
  them with `<script src>`.

## Key gotchas (already proven / to handle)
- **Endpoint names change** to `blueprint.func` (e.g. `healthz` → `health.healthz`).
  Update `_OPEN_ENDPOINTS` and every `url_for(...)` accordingly. (Handled for health.)
- **No import cycles**: blueprint modules import from `core/`, never from `app.py`.
  Register blueprints in `app.py` after `app` is created.
- **Jinja-in-JS**: the inline JS references `{{ ... }}` template values, so JS
  can't be moved verbatim. Plan: emit a single `window.__CONFIG__ = {...}` JSON
  bootstrap in the template, then the extracted static JS reads from it. Move JS
  in slices, one tab/feature at a time.
- **Auth gate stays central** and keeps using `request.endpoint` against the
  allowlist; verify each moved route's new endpoint name is covered.

## Migration order (one CI-gated PR each)
1. `health` blueprint ✅ (this branch).
2. Extract `core/errors.py` + `core/auth.py`; repoint app.py.
3. `webhooks` blueprint (Graph notifications + subscriptions).
4. `freight`, then `production`, then `pending_pos`, then `inventory`, then `admin`.
5. JS extraction in slices behind `window.__CONFIG__`.

## Testing / rollback
- Every slice: `python -m compileall`, `pytest` (the 3 suites), `safe_push.py`,
  boot check via Flask test client, then PR. CI (.github/workflows/ci.yml) gates merge.
- Rollback for any slice = don't merge the PR. Production (`main`) is untouched
  until a slice is reviewed + merged.
