"""
pipeline.py — Orchestrates the full data fetch cycle.

Two modes:
  1. backfill()  — First-time run. Resolves all tickers to instrument IDs,
                   fetches INITIAL_CANDLES_COUNT daily candles per ticker,
                   and stores everything in SQLite.

  2. refresh()   — Daily run (called by scheduler or manually). Only fetches
                   tickers whose cache is stale. Grabs the last
                   REFRESH_CANDLES_COUNT candles and upserts them.

Both functions are safe to run multiple times (idempotent).
"""

import logging

from config import (
    WATCHLIST_TICKERS,
    INITIAL_CANDLES_COUNT,
    REFRESH_CANDLES_COUNT,
    CACHE_TTL_HOURS,
    DAILY_INTERVAL,
)
from etoro_client import EToroClient, EToroAPIError
from database import (
    initialise_db,
    upsert_instrument,
    upsert_candles,
    update_fetch_log,
    get_instrument_id,
    is_stale,
    get_portfolio_summary,
)

log = logging.getLogger(__name__)


def backfill(tickers: list[str] = None):
    """
    Full historical backfill for all (or specified) tickers.

    Resolves ticker → instrument ID, then fetches INITIAL_CANDLES_COUNT daily
    candles per ticker. Safe to re-run — existing rows are updated, not duplicated.

    Args:
        tickers: Optional list of tickers to backfill. Defaults to WATCHLIST_TICKERS.
    """
    tickers = tickers or WATCHLIST_TICKERS
    client  = EToroClient()
    initialise_db()

    log.info(f"Starting backfill for {len(tickers)} tickers: {tickers}")

    for ticker in tickers:
        try:
            _ensure_instrument_id(client, ticker)
            instrument_id = get_instrument_id(ticker)

            if not instrument_id:
                log.error(f"Could not resolve instrument ID for {ticker}. Skipping.")
                continue

            log.info(f"Fetching {INITIAL_CANDLES_COUNT} daily candles for {ticker} (ID: {instrument_id})")
            candles = client.get_candles(
                instrument_id=instrument_id,
                interval=DAILY_INTERVAL,
                candles_count=INITIAL_CANDLES_COUNT,
                direction="desc",
            )

            upsert_candles(ticker, instrument_id, candles)
            update_fetch_log(ticker)
            log.info(f"✓ {ticker}: {len(candles)} candles stored")

        except EToroAPIError as exc:
            log.error(f"API error for {ticker}: {exc}")
        except Exception as exc:
            log.exception(f"Unexpected error for {ticker}: {exc}")

    _print_summary()


def refresh(tickers: list[str] = None):
    """
    Daily refresh: only fetches tickers whose cache is stale.

    Designed to be called by the scheduler every evening after market close.
    Fetches the last REFRESH_CANDLES_COUNT candles and upserts any new rows.

    Args:
        tickers: Optional list of tickers to refresh. Defaults to WATCHLIST_TICKERS.
    """
    tickers = tickers or WATCHLIST_TICKERS
    client  = EToroClient()
    initialise_db()

    stale    = [t for t in tickers if is_stale(t, CACHE_TTL_HOURS)]
    fresh    = [t for t in tickers if not is_stale(t, CACHE_TTL_HOURS)]

    if fresh:
        log.info(f"Already fresh (skipping): {fresh}")
    if not stale:
        log.info("All tickers are up to date. Nothing to fetch.")
        return

    log.info(f"Refreshing {len(stale)} stale tickers: {stale}")

    for ticker in stale:
        try:
            _ensure_instrument_id(client, ticker)
            instrument_id = get_instrument_id(ticker)

            if not instrument_id:
                log.error(f"No instrument ID for {ticker}. Run backfill() first.")
                continue

            candles = client.get_candles(
                instrument_id=instrument_id,
                interval=DAILY_INTERVAL,
                candles_count=REFRESH_CANDLES_COUNT,
                direction="desc",
            )

            upsert_candles(ticker, instrument_id, candles)
            update_fetch_log(ticker)
            log.info(f"✓ {ticker}: refreshed {len(candles)} candles")

        except EToroAPIError as exc:
            log.error(f"API error for {ticker}: {exc}")
        except Exception as exc:
            log.exception(f"Unexpected error for {ticker}: {exc}")

    _print_summary()


def refresh_single(ticker: str, candles_count: int = None, force: bool = False):
    """
    Manually refresh a single ticker — useful for on-demand lookups.

    Args:
        ticker:        Ticker to fetch (e.g. "NVDA")
        candles_count: How many candles to fetch (defaults to REFRESH_CANDLES_COUNT)
        force:         If True, fetches even if the cache is still fresh
    """
    if not force and not is_stale(ticker, CACHE_TTL_HOURS):
        log.info(f"{ticker} cache is fresh. Use force=True to override.")
        return

    client        = EToroClient()
    candles_count = candles_count or REFRESH_CANDLES_COUNT

    initialise_db()
    _ensure_instrument_id(client, ticker)
    instrument_id = get_instrument_id(ticker)

    if not instrument_id:
        raise ValueError(f"Could not resolve instrument ID for {ticker}")

    candles = client.get_candles(
        instrument_id=instrument_id,
        interval=DAILY_INTERVAL,
        candles_count=candles_count,
        direction="desc",
    )

    upsert_candles(ticker, instrument_id, candles)
    update_fetch_log(ticker)
    log.info(f"✓ {ticker}: {len(candles)} candles fetched and stored")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _ensure_instrument_id(client: EToroClient, ticker: str):
    """
    Look up and cache the eToro instrument ID for a ticker if we don't have it yet.
    Does nothing if the ID is already in the database.
    """
    existing = get_instrument_id(ticker)
    if existing:
        log.debug(f"{ticker} already resolved → {existing}")
        return

    log.info(f"Resolving {ticker} via eToro search API...")
    instrument = client.search_instrument(ticker)

    if not instrument:
        log.error(f"eToro returned no results for ticker: {ticker}")
        return

    # Field names from the actual search API response schema
    instrument_id = (
        instrument.get("instrumentID")
        or instrument.get("instrumentId")
        or instrument.get("internalInstrumentId")
    )
    display_name = (
        instrument.get("instrumentDisplayName")
        or instrument.get("displayname")
        or instrument.get("displayName")
        or ticker
    )
    exchange = str(
        instrument.get("exchangeID")
        or ""
    )

    if not instrument_id:
        log.error(f"No instrument ID in eToro response for {ticker}: {instrument}")
        return

    upsert_instrument(ticker, int(instrument_id), str(display_name), str(exchange))
    log.info(f"Resolved {ticker} → instrument ID {instrument_id}")


def _print_summary():
    """Log a quick portfolio summary table after each run."""
    summary = get_portfolio_summary()
    if not summary:
        return

    log.info("\n" + "─" * 62)
    log.info(f"{'Ticker':<8} {'Date':<12} {'Close':>8} {'Prev':>8} {'Chg%':>7}")
    log.info("─" * 62)
    for row in summary:
        chg  = row.get("day_change_pct")
        sign = "+" if chg and chg > 0 else ""
        log.info(
            f"{row['ticker']:<8} "
            f"{row.get('latest_date', 'N/A'):<12} "
            f"{row.get('latest_close', 0):>8.2f} "
            f"{row.get('prev_close', 0):>8.2f} "
            f"{sign}{chg or 0:>6.2f}%"
        )
    log.info("─" * 62 + "\n")
