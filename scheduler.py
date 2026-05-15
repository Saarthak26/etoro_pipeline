"""
scheduler.py — Runs the daily data refresh automatically.

Starts a background APScheduler job that calls pipeline.refresh() every evening
at the time configured in config.py (default: 23:00 Europe/Berlin, safely after
US market close at 22:00 Berlin time).

Run this process and leave it running — it handles itself.
Use Ctrl+C or send SIGTERM to shut down gracefully.
"""

import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config import SCHEDULER_HOUR, SCHEDULER_MINUTE, SCHEDULER_TZ
from pipeline import refresh

log = logging.getLogger(__name__)


def run_scheduler():
    """Start the blocking APScheduler process."""
    scheduler = BlockingScheduler(timezone=SCHEDULER_TZ)

    scheduler.add_job(
        func=refresh,
        trigger=CronTrigger(
            hour=SCHEDULER_HOUR,
            minute=SCHEDULER_MINUTE,
            timezone=SCHEDULER_TZ,
        ),
        id="daily_market_refresh",
        name="eToro daily OHLCV refresh",
        replace_existing=True,
        misfire_grace_time=3600,   # If the job fires late by up to 1 hour, still run it
    )

    # ── Graceful shutdown on SIGTERM / Ctrl+C ─────────────────────────────────
    def shutdown(signum, frame):
        log.info("Shutdown signal received. Stopping scheduler...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    log.info(
        f"Scheduler started. Daily refresh runs at "
        f"{SCHEDULER_HOUR:02d}:{SCHEDULER_MINUTE:02d} {SCHEDULER_TZ}. "
        f"Press Ctrl+C to stop."
    )

    scheduler.start()
