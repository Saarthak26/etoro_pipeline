"""
scheduler.py — Runs the trading dashboard refresh automatically.

Jobs:
  1. Market open  — 15:30 Berlin  Mon–Fri  → full export + pre-breakout Screener tab
  2. Hourly       — 16:30–21:30 Berlin Mon–Fri → live-only export (Overview widget + Daily Perf + per-ticker tabs)
  3. Market close — 22:00 Berlin  Mon–Fri  → full export + pre-breakout Screener tab

The Screener tab (ranking + backtest + charts) is refreshed only at open and close
— it trains a model + runs the walk-forward, so it is deliberately kept off the
hourly and midnight cadences (see run_export's SCREENER_TRIGGERS in sheets_exporter.py).
  4. Daily OHLCV  — 23:00 Berlin  daily    → position sync + candle refresh + full export
  5. NY midnight  — 00:00 New York daily   → full export
  6. Pre-open sync — 15:25 Berlin Mon–Fri  → eToro position sync (5 min before NYSE open)
  7. Pre-close sync — 21:55 Berlin Mon–Fri → eToro position sync (5 min before NYSE close)

Every DB-writing job holds an advisory file lock (pipeline_lock.py), so a manual
CLI command (export/backfill/…) and a scheduled job can never write the database
concurrently — the failure mode that can corrupt SQLite.

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
    GOOGLE_SHEET_ID,
)
from pipeline import refresh, sync_positions
from pipeline_lock import pipeline_lock, PipelineBusyError

log = logging.getLogger(__name__)


def _locked(label: str, fn):
    """Run a scheduled job while holding the DB write-lock. If a manual pipeline
    command (or another job) holds it, skip this run rather than risk a concurrent
    write — the next scheduled fire will catch up."""
    try:
        with pipeline_lock(label=f"scheduler:{label}"):
            fn()
    except PipelineBusyError:
        log.warning("Skipping scheduled '%s' — another pipeline operation is "
                    "running; will run at the next scheduled time.", label)


def _sync_positions_job():
    """Standalone daily position sync (broker → positions.json)."""
    try:
        sync_positions()
    except Exception:
        log.exception("Daily position sync failed")


def _daily_refresh():
    """Sync live positions, refresh OHLCV data, then push a full export."""
    try:
        sync_positions()
    except Exception:
        log.exception("Position sync failed — continuing with OHLCV refresh")
    refresh()
    _sheets_export("daily_refresh")


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

    # ── 1. Market open — 15:30 Berlin (09:30 NY) — FULL export ───────────────
    scheduler.add_job(
        func=lambda: _locked("market_open", lambda: _sheets_export("market_open")),
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=15, minute=30,
            timezone=SCHEDULER_TZ,
        ),
        id="sheets_market_open",
        name="Google Sheets — market open (full)",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # ── 2. Hourly — 16:30–21:30 Berlin — LIVE-ONLY export ────────────────────
    scheduler.add_job(
        func=lambda: _locked("hourly", lambda: _sheets_export("hourly")),
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="16-21", minute=30,
            timezone=SCHEDULER_TZ,
        ),
        id="sheets_export_hourly",
        name="Google Sheets — hourly live update",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # ── 3. Market close — 22:00 Berlin (16:00 NY) — FULL export ──────────────
    scheduler.add_job(
        func=lambda: _locked("market_close", lambda: _sheets_export("market_close")),
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=22, minute=0,
            timezone=SCHEDULER_TZ,
        ),
        id="sheets_market_close",
        name="Google Sheets — market close (full)",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # ── 4. Position sync + OHLCV refresh — 23:00 Berlin — FULL export ─────────
    scheduler.add_job(
        func=lambda: _locked("daily_refresh", _daily_refresh),
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

    # ── 5. NY midnight — 00:00 America/New_York — FULL export ────────────────
    scheduler.add_job(
        func=lambda: _locked("ny_midnight", lambda: _sheets_export("ny_midnight")),
        trigger=CronTrigger(
            hour=0, minute=0,
            timezone="America/New_York",
        ),
        id="sheets_ny_midnight",
        name="Google Sheets — NY midnight (full)",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # ── 6. Position sync — 5 min before NYSE open (09:30 ET = 15:30 Berlin) ──
    #     Runs just ahead of the open/close full exports so positions.json is
    #     fresh going in, and offset a few minutes so it never races those jobs
    #     for the write-lock.
    scheduler.add_job(
        func=lambda: _locked("sync_open", _sync_positions_job),
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=15, minute=25,
            timezone=SCHEDULER_TZ,
        ),
        id="sync_positions_open",
        name="eToro position sync — pre-open (NYSE 09:30 ET)",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # ── 7. Position sync — 5 min before NYSE close (16:00 ET = 22:00 Berlin) ──
    scheduler.add_job(
        func=lambda: _locked("sync_close", _sync_positions_job),
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=21, minute=55,
            timezone=SCHEDULER_TZ,
        ),
        id="sync_positions_close",
        name="eToro position sync — pre-close (NYSE 16:00 ET)",
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
        "  • Pre-open sync (positions)   : 15:25 %s (Mon–Fri, NYSE open)\n"
        "  • Market open  (full export)  : 15:30 %s (Mon–Fri)\n"
        "  • Hourly       (live-only)    : 16:30–21:30 %s (Mon–Fri)\n"
        "  • Pre-close sync (positions)  : 21:55 %s (Mon–Fri, NYSE close)\n"
        "  • Market close (full export)  : 22:00 %s (Mon–Fri)\n"
        "  • Daily OHLCV  (full export)  : %02d:%02d %s\n"
        "  • NY midnight  (full export)  : 00:00 America/New_York (daily)\n"
        "All DB-writing jobs hold an advisory lock — a manual pipeline command and a\n"
        "scheduled job can never write the database at the same time.\n"
        "Press Ctrl+C to stop.",
        SCHEDULER_TZ, SCHEDULER_TZ, SCHEDULER_TZ, SCHEDULER_TZ, SCHEDULER_TZ,
        SCHEDULER_HOUR, SCHEDULER_MINUTE, SCHEDULER_TZ,
    )

    scheduler.start()
