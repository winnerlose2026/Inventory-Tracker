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
    listening (the localhost endpoints the jobs call are reachable by then).

    Logs a confirmation through gunicorn's own logger (which is configured at
    INFO, unlike a bare module logger) so it always shows in the deploy logs."""
    try:
        from scheduler import start_scheduler

        sched = start_scheduler()
        if sched is None:
            server.log.info("in-process scheduler disabled (INPROCESS_SCHEDULER=off)")
            return
        jobs = ", ".join(
            f"{job.id}@{job.next_run_time:%H:%M %Z}" for job in sched.get_jobs()
        )
        server.log.info("in-process scheduler started: %s", jobs)
    except Exception as exc:  # noqa: BLE001 - never let a scheduler hiccup stop boot
        server.log.exception("in-process scheduler failed to start: %s", exc)
