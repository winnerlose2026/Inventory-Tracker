# Superseded

This handoff described a `/api/email/scan` 500 from spring 2026 (auto-deploy
off, a dashboard start command without `--timeout`). Both were resolved long
ago: the service starts with `gunicorn -c gunicorn.conf.py app:app`, and
deploys come from the CI deploy hook.

For how the service runs today (jobs, environment variables, alerts, backups
and restore) see [`docs/OPERATIONS.md`](../OPERATIONS.md). For how to make a
change, see [`CLAUDE.md`](../../CLAUDE.md).
