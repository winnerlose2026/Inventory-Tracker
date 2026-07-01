"""Gunicorn configuration for bagel-inventory.

Preserves the previous worker/thread/timeout profile and starts the
in-process scheduler exactly once, in the arbiter, via the when_ready hook —
so the web workers do not each schedule the daily jobs. See scheduler.py.

Used by both the Procfile and render.yaml:
    gunicorn -c gunicorn.conf.py app:app
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "180"))


def when_ready(server):
    """Start the in-process scheduler once the master is up and workers are
    listening (the localhost endpoints the jobs call are reachable by then)."""
    try:
        from scheduler import start_scheduler

        start_scheduler()
    except Exception as exc:  # noqa: BLE001 - never let a scheduler hiccup stop boot
        server.log.error("in-process scheduler failed to start: %s", exc)
