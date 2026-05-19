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

from config import (
    SCHEDULER_HOUR, SCHEDULER_MINUTE, SCHEDULER_TZ,
    SHEETS_OPEN_HOUR, SHEETS_OPEN_MINUTE,
    SHEETS_CLOSE_HOUR, SHEETS_CLOSE_MINUTE,
    SHEETS_MARKET_TZ,
    GOOGLE_SHEETS_CREDENTIALS_PATH, GOOGLE_SHEET_ID,
)
from pipeline import refresh, sync_positions

log = logging.getLogger(__name__)


def _daily_refresh():
    """Sync live positions then refresh OHLCV data."""
    try:
        sync_positions()
    except Exception:
        log.exception("Position sync failed — continuing with OHLCV refresh")
    refresh()


def _sheets_export(trigger: str):
    """Wrapper so the scheduler can pass a trigger label to the exporter."""
    if not GOOGLE_SHEET_ID:
        log.warning("GOOGLE_SHEET_ID is not set — skipping Sheets export. Run: python main.py setup-sheets")
        return
    try:
        from sheets_exporter import run_export
        run_export(trigger=trigger)
    except Exception:
        log.exception("Google Sheets export failed (trigger=%s)", trigger)


def run_scheduler():
    """Start the blocking APScheduler process."""
    scheduler = BlockingScheduler(timezone=SCHEDULER_TZ)

    # ── 1. Position sync + OHLCV refresh (23:00 Berlin, after US market close) ──
    scheduler.add_job(
        func=_daily_refresh,
        trigger=CronTrigger(
            hour=SCHEDULER_HOUR,
            minute=SCHEDULER_MINUTE,
            timezone=SCHEDULER_TZ,
        ),
        id="daily_market_refresh",
        name="eToro position sync + OHLCV refresh",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # ── 2. Google Sheets export at market open (09:30 New York, weekdays) ─────
    scheduler.add_job(
        func=lambda: _sheets_export("market_open"),
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=SHEETS_OPEN_HOUR,
            minute=SHEETS_OPEN_MINUTE,
            timezone=SHEETS_MARKET_TZ,
        ),
        id="sheets_export_open",
        name="Google Sheets export — market open",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # ── 3. Google Sheets export at market close (16:00 New York, weekdays) ────
    scheduler.add_job(
        func=lambda: _sheets_export("market_close"),
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=SHEETS_CLOSE_HOUR,
            minute=SHEETS_CLOSE_MINUTE,
            timezone=SHEETS_MARKET_TZ,
        ),
        id="sheets_export_close",
        name="Google Sheets export — market close",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # ── Graceful shutdown on SIGTERM / Ctrl+C ─────────────────────────────────
    def shutdown(signum, frame):
        log.info("Shutdown signal received. Stopping scheduler...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    log.info(
        "Scheduler started.\n"
        "  • Position sync + OHLCV refresh : %02d:%02d %s (daily)\n"
        "  • Sheets open                   : %02d:%02d %s (Mon–Fri)\n"
        "  • Sheets close                  : %02d:%02d %s (Mon–Fri)\n"
        "Press Ctrl+C to stop.",
        SCHEDULER_HOUR, SCHEDULER_MINUTE, SCHEDULER_TZ,
        SHEETS_OPEN_HOUR, SHEETS_OPEN_MINUTE, SHEETS_MARKET_TZ,
        SHEETS_CLOSE_HOUR, SHEETS_CLOSE_MINUTE, SHEETS_MARKET_TZ,
    )

    scheduler.start()
