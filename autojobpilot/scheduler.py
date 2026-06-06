"""Scheduler (APScheduler) — cron-style, no broker.

Fires run_cycle at the configured AEST times. max_instances=1 + coalesce prevent
overlapping runs (PRD Feature 13).

Run it as a STANDALONE DAEMON so automated runs fire independently of the
dashboard (the embedded-in-Streamlit scheduler only runs while a browser session
is connected):

    python -m autojobpilot.scheduler        # blocks, fires the daily run

Keep exactly one scheduler running (the daemon) to avoid double-firing.
"""

from __future__ import annotations

import time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Config, load_config
from .pipeline import run_cycle
from .utils import get_logger

log = get_logger("scheduler")


def _slot_to_run_type(hour: int) -> str:
    return "afternoon" if hour < 18 else "night"


def build_scheduler(cfg: Config) -> BackgroundScheduler:
    tz = ZoneInfo(cfg.schedule.get("timezone", "Australia/Sydney"))
    sched = BackgroundScheduler(timezone=tz, job_defaults={
        "max_instances": 1,   # never overlap runs
        "coalesce": True,     # collapse missed runs into one
        "misfire_grace_time": 3600,
    })
    for slot in cfg.schedule.get("runs", []):
        hh, mm = (int(x) for x in slot.split(":"))
        run_type = _slot_to_run_type(hh)
        sched.add_job(
            _safe_run, "cron", hour=hh, minute=mm,
            args=[cfg, run_type], id=f"run_{slot.replace(':', '')}",
            replace_existing=True,
        )
        log.info("Scheduled %s run at %02d:%02d %s", run_type, hh, mm,
                 cfg.schedule.get("timezone"))
    return sched


def _safe_run(cfg: Config, run_type: str) -> None:
    """Wrapper so a failing run never kills the scheduler thread.

    run_cycle already does 3 retries with backoff internally; this only guards
    against the final raise so the scheduler keeps serving the next slot.
    """
    try:
        run_cycle(cfg, run_type)
    except Exception:
        log.exception("Scheduled %s run failed after all retries", run_type)


def run_daemon() -> None:
    """Start the scheduler and block forever — the standalone automated runner."""
    cfg = load_config()
    sched = build_scheduler(cfg)
    sched.start()
    runs = cfg.schedule.get("runs", [])
    log.info("Scheduler daemon started; daily run(s) at %s %s. Ctrl-C to stop.",
             ", ".join(runs), cfg.schedule.get("timezone"))
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()
        log.info("Scheduler daemon stopped.")


if __name__ == "__main__":
    run_daemon()
