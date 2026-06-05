from __future__ import annotations

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

import json
import logging
from datetime import datetime, timezone

from config import (
    WATCHLIST_TICKERS,
    INITIAL_CANDLES_COUNT,
    REFRESH_CANDLES_COUNT,
    CACHE_TTL_HOURS,
    DAILY_INTERVAL,
    POSITIONS_PATH,
)
from etoro_client import EToroClient, EToroAPIError
from database import (
    initialise_db,
    upsert_instrument,
    upsert_candles,
    update_fetch_log,
    get_instrument_id,
    get_ticker_by_instrument_id,
    is_stale,
    get_portfolio_summary,
    save_closed_position,
)

log = logging.getLogger(__name__)


def sync_positions():
    """
    Pull live open positions from the eToro portfolio API and rewrite positions.json.

    Each open position becomes one entry (preserving per-lot granularity).
    Watchlist tickers with no open positions are kept as zero-unit placeholders
    so they still appear in the sheets export.

    Safe to run repeatedly — positions.json is fully overwritten each time.
    """
    client = EToroClient()
    initialise_db()

    log.info("Syncing positions from eToro portfolio API...")
    raw_positions = client.get_portfolio()
    log.info(f"Received {len(raw_positions)} open positions from eToro")

    # Build instrumentID → ticker using the local DB, with API fallback for unknowns
    id_to_ticker: dict[int, str] = {}
    for pos in raw_positions:
        iid = pos["instrumentID"]
        if iid in id_to_ticker:
            continue
        ticker = get_ticker_by_instrument_id(iid)
        if ticker is None:
            ticker = _resolve_unknown_instrument(client, iid)
        if ticker:
            id_to_ticker[iid] = ticker
        else:
            log.warning(f"Could not resolve ticker for instrumentID {iid} — skipping")

    # ── Detect closed positions by comparing to previous snapshot ─────────────
    new_position_ids = {str(p["positionID"]) for p in raw_positions if p.get("positionID")}
    try:
        with open(POSITIONS_PATH) as f:
            old_entries = json.load(f)
        old_ids = {str(e["position_id"]) for e in old_entries if e.get("position_id")}
        closed_ids = old_ids - new_position_ids
        if closed_ids:
            log.info(f"Detected {len(closed_ids)} closed position(s): {closed_ids}")
            for old_e in old_entries:
                pid = str(old_e.get("position_id", ""))
                if pid not in closed_ids:
                    continue
                ticker     = old_e.get("ticker", "")
                units      = float(old_e.get("units") or 0)
                open_price = float(old_e.get("open_price") or 0)
                direction  = old_e.get("direction", "BUY")
                # Best-effort close price from the most recent daily candle
                from database import get_latest_close
                latest = get_latest_close(ticker)
                close_price = float(latest["close"]) if latest else 0.0
                realized = ((close_price - open_price) * units
                            if direction == "BUY"
                            else (open_price - close_price) * units)
                save_closed_position({
                    "position_id": pid,
                    "ticker":      ticker,
                    "direction":   direction,
                    "units":       units,
                    "open_price":  open_price,
                    "open_date":   old_e.get("open_date", ""),
                    "close_price": close_price,
                    "close_date":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "realized_pnl": round(realized, 2),
                    "fees":        float(old_e.get("total_fees") or 0),
                    "source":      "auto",
                })
                log.info(f"Saved closed position: {ticker} pid={pid} P&L=${realized:.2f}")
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass   # No previous positions.json or missing position_id — skip detection

    # ── Build the new positions list ───────────────────────────────────────────
    active_tickers: set[str] = set()
    entries: list[dict] = []

    for pos in raw_positions:
        iid    = pos["instrumentID"]
        ticker = id_to_ticker.get(iid)
        if not ticker:
            continue

        active_tickers.add(ticker)
        open_dt   = _parse_open_date(pos.get("openDateTime", ""))
        no_sl     = pos.get("isNoStopLoss", True)
        no_tp     = pos.get("isNoTakeProfit", True)

        entries.append({
            "ticker":      ticker,
            "position_id": str(pos.get("positionID", "")),
            "direction":   "BUY" if pos.get("isBuy", True) else "SELL",
            "units":       pos["units"],
            "open_price":  pos["openRate"],
            "open_date":   open_dt,
            "stop_loss":   None if no_sl else pos.get("stopLossRate"),
            "take_profit": None if no_tp else pos.get("takeProfitRate"),
            "total_fees":  pos.get("totalFees", 0),
        })

    # Append zero-unit placeholders for watchlist tickers not currently held
    for ticker in WATCHLIST_TICKERS:
        if ticker not in active_tickers:
            entries.append({
                "ticker":      ticker,
                "position_id": "",
                "direction":   "BUY",
                "units":       0,
                "open_price":  0.0,
                "open_date":   "",
                "stop_loss":   None,
                "take_profit": None,
                "total_fees":  0,
            })

    with open(POSITIONS_PATH, "w") as f:
        json.dump(entries, f, indent=2)

    held = len([e for e in entries if e["units"] > 0])
    log.info(f"positions.json updated: {held} open positions across {len(active_tickers)} tickers")


def _parse_open_date(dt_str: str) -> str:
    """Convert ISO datetime from the API ('2026-04-14T16:26:36.687Z') to 'D-M-YYYY'."""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return f"{dt.day}-{dt.month}-{dt.year}"
    except Exception:
        return dt_str[:10]


def _resolve_unknown_instrument(client: EToroClient, instrument_id: int) -> str | None:
    """
    Look up an instrument ID that is not yet in the local DB.
    If found, persists it to the DB for future lookups.
    """
    inst = client.get_instrument_by_id(instrument_id)
    if not inst:
        return None

    ticker = inst.get("symbolFull") or inst.get("symbol")
    if not ticker:
        return None

    iid          = inst.get("instrumentID") or inst.get("internalInstrumentId") or instrument_id
    display_name = inst.get("instrumentDisplayName") or inst.get("displayName") or ticker
    exchange     = str(inst.get("exchangeID") or "")

    upsert_instrument(ticker.upper(), int(iid), str(display_name), exchange)
    log.info(f"Resolved unknown instrumentID {instrument_id} → {ticker}")
    return ticker.upper()


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
