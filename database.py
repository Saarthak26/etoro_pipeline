from __future__ import annotations

"""
database.py — SQLite schema and read/write helpers for the market data cache.

Tables:
  instruments   — ticker → instrumentId mapping (fetched once, reused forever)
  daily_candles — OHLCV at OneDay granularity (7-day rolling + full history)
  fetch_log     — timestamp of last successful fetch per ticker (cache TTL check)

All writes use INSERT OR REPLACE so re-runs are safe and idempotent.
"""

import sqlite3
import logging
from datetime import datetime, timezone

from config import DB_PATH

log = logging.getLogger(__name__)


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    ticker          TEXT PRIMARY KEY,
    instrument_id   INTEGER NOT NULL,
    display_name    TEXT,
    exchange        TEXT,
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_candles (
    instrument_id   INTEGER NOT NULL,
    ticker          TEXT NOT NULL,
    date            TEXT NOT NULL,      -- ISO date: 2025-05-14
    open            REAL,
    high            REAL,
    low             REAL,
    close           REAL,
    volume          REAL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (instrument_id, date)
);

CREATE INDEX IF NOT EXISTS idx_daily_candles_ticker_date
    ON daily_candles (ticker, date DESC);

CREATE TABLE IF NOT EXISTS fetch_log (
    ticker          TEXT PRIMARY KEY,
    last_fetch_utc  TEXT NOT NULL       -- ISO 8601 UTC timestamp of last successful pull
);
"""


# ── Connection helper ─────────────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    """Return a sqlite3 connection with row_factory set to dict-like access."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # Better concurrent read performance
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialise_db():
    """Create tables and indexes if they don't already exist."""
    with get_connection() as conn:
        conn.executescript(SCHEMA)
    log.info(f"Database ready at: {DB_PATH}")


# ── Instruments table ─────────────────────────────────────────────────────────

def upsert_instrument(ticker: str, instrument_id: int, display_name: str = "", exchange: str = ""):
    """Save or update the ticker → instrument ID mapping."""
    now = _utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO instruments
                (ticker, instrument_id, display_name, exchange, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ticker, instrument_id, display_name, exchange, now),
        )
    log.debug(f"Upserted instrument: {ticker} → {instrument_id}")


def get_instrument_id(ticker: str) -> int | None:
    """Return the cached eToro instrument ID for a ticker, or None if not cached."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT instrument_id FROM instruments WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    return int(row["instrument_id"]) if row else None


def get_all_instruments() -> list[dict]:
    """Return all cached ticker → ID mappings."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ticker, instrument_id, display_name, exchange FROM instruments ORDER BY ticker"
        ).fetchall()
    return [dict(r) for r in rows]


def get_ticker_by_instrument_id(instrument_id: int) -> str | None:
    """Return the ticker for a given eToro instrument ID, or None if not cached."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT ticker FROM instruments WHERE instrument_id = ?",
            (instrument_id,),
        ).fetchone()
    return row["ticker"] if row else None


# ── Daily candles table ───────────────────────────────────────────────────────

def upsert_candles(ticker: str, instrument_id: int, candles: list[dict]):
    """
    Insert or update a batch of daily OHLCV candles.

    Args:
        ticker:        e.g. "NVDA"
        instrument_id: eToro numeric ID
        candles:       List of candle dicts from EToroClient.get_candles()
                       Each has: fromDate, open, high, low, close, volume
    """
    if not candles:
        log.warning(f"No candles to upsert for {ticker}")
        return

    now  = _utc_now()
    rows = []
    for c in candles:
        date_str = _parse_date(c.get("fromDate", ""))
        if not date_str:
            continue
        rows.append((
            instrument_id,
            ticker,
            date_str,
            c.get("open"),
            c.get("high"),
            c.get("low"),
            c.get("close"),
            c.get("volume"),
            now,
        ))

    with get_connection() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO daily_candles
                (instrument_id, ticker, date, open, high, low, close, volume, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    log.info(f"Upserted {len(rows)} candles for {ticker}")


def get_candles(ticker: str, days: int = 30) -> list[dict]:
    """
    Retrieve the most recent N daily candles for a ticker from the local cache.

    Args:
        ticker: e.g. "NVDA"
        days:   How many trading days to return

    Returns:
        List of dicts with keys: date, open, high, low, close, volume
        Ordered oldest → newest.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT date, open, high, low, close, volume
            FROM   daily_candles
            WHERE  ticker = ?
            ORDER  BY date DESC
            LIMIT  ?
            """,
            (ticker, days),
        ).fetchall()

    # Reverse so the result is chronological (oldest first) for charting
    return [dict(r) for r in reversed(rows)]


def get_latest_close(ticker: str) -> dict | None:
    """Return the most recent closing price and date for a ticker."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT date, close, open, high, low, volume
            FROM   daily_candles
            WHERE  ticker = ?
            ORDER  BY date DESC
            LIMIT  1
            """,
            (ticker,),
        ).fetchone()
    return dict(row) if row else None


def get_candles_range(ticker: str, from_date: str, to_date: str) -> list[dict]:
    """
    Return candles for a specific date range (inclusive).

    Args:
        ticker:    e.g. "NVDA"
        from_date: ISO date string "2025-01-01"
        to_date:   ISO date string "2025-05-15"
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT date, open, high, low, close, volume
            FROM   daily_candles
            WHERE  ticker = ?
              AND  date BETWEEN ? AND ?
            ORDER  BY date ASC
            """,
            (ticker, from_date, to_date),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Fetch log (cache TTL) ─────────────────────────────────────────────────────

def update_fetch_log(ticker: str):
    """Record that we just successfully fetched data for this ticker."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO fetch_log (ticker, last_fetch_utc)
            VALUES (?, ?)
            """,
            (ticker, _utc_now()),
        )


def is_stale(ticker: str, ttl_hours: float) -> bool:
    """
    Return True if the ticker has never been fetched or the last fetch was
    more than ttl_hours ago.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_fetch_utc FROM fetch_log WHERE ticker = ?",
            (ticker,),
        ).fetchone()

    if not row:
        return True

    last_fetch = datetime.fromisoformat(row["last_fetch_utc"]).replace(tzinfo=timezone.utc)
    age_hours  = (datetime.now(timezone.utc) - last_fetch).total_seconds() / 3600
    return age_hours > ttl_hours


# ── Summary query (for dashboard and logging) ─────────────────────────────────

def get_portfolio_summary() -> list[dict]:
    """
    Return a one-row-per-ticker summary with the latest close, previous close,
    and simple day-over-day change.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT
                    ticker,
                    date,
                    close,
                    open,
                    high,
                    low,
                    volume,
                    ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
                FROM daily_candles
            )
            SELECT
                today.ticker,
                today.date       AS latest_date,
                today.close      AS latest_close,
                today.open       AS latest_open,
                today.high       AS latest_high,
                today.low        AS latest_low,
                today.volume     AS latest_volume,
                prev.close       AS prev_close,
                ROUND(
                    (today.close - prev.close) / prev.close * 100, 2
                )                AS day_change_pct
            FROM ranked today
            LEFT JOIN ranked prev
                ON prev.ticker = today.ticker AND prev.rn = 2
            WHERE today.rn = 1
            ORDER BY today.ticker
            """
        ).fetchall()
    return [dict(r) for r in rows]


# ── Internal utilities ────────────────────────────────────────────────────────

def _utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _parse_date(from_date: str) -> str | None:
    """
    Extract just the date portion (YYYY-MM-DD) from a datetime string.

    eToro returns fromDate as "2025-03-05T10:34:00Z" for intraday
    and "2025-03-05T00:00:00Z" for daily candles.
    """
    if not from_date:
        return None
    try:
        return from_date[:10]   # "2025-03-05"
    except Exception:
        log.warning(f"Could not parse date: {from_date}")
        return None
