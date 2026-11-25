---
name: steward
description: House rules for PR Steward and /ship watches in this repository
user-invocable: false
disable-model-invocation: true
---

# PR Steward rules: Inventory Tracker

Read `CLAUDE.md` first. Its "Rules that have each cost us data or an outage" section applies to every commit you push. This app is live with real stock data, and a merge to `main` deploys to Render.

## Before every push
- `python -m pytest -q` and `python scripts/safe_push.py` must both pass (`pip install -r requirements.txt pyyaml pytest`).
- Every fix gets at least one test.
- If you add or remove a route, regenerate the pinned list with `python tests/test_route_manifest.py --write` and commit it.

## Don't
- Don't commit anything under `data/`, and don't commit `.env`.
- Don't make a new environment variable mandatory. Don't rename or remove an existing API response field.
- Don't loosen the shrink guards (`allow_shrink`), the data lock, or the deploy and health-check steps in `ci.yml` to get a check green.
- Don't deploy by hand, and don't push to `main`.
- Commit messages follow the format in `CLAUDE.md`: an imperative summary of 72 characters or fewer, then what changed and why.
