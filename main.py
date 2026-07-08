"""
main.py — Entry point for the eToro data pipeline.

Commands:
    python main.py backfill              — First-time full history fetch for all tickers
    python main.py refresh               — Incremental daily refresh (stale tickers only)
    python main.py refresh NVDA AMD      — Refresh specific tickers only
    python main.py sync-positions        — Pull live positions from eToro and rewrite positions.json
    python main.py account-summary       — Read-only diagnostic: dump eToro broker-authoritative account snapshot
    python main.py query NVDA            — Print last 10 candles for a ticker
    python main.py summary               — Print the latest close for every ticker
    python main.py scheduler             — Start the background daily refresh daemon
    python main.py setup-sheets          — One-time Google Sheets setup wizard
    python main.py export                — Manually push all data to Google Sheets now
    python main.py import-statement <f>  — Import closed trades from an eToro account statement CSV
    python main.py update-macro [TICK…] — Refresh daily macro cache (news, analyst targets) for all or specific tickers
    python main.py screener-backfill market — Backfill the whole liquid US market from yfinance (~6.9k names, one-time)
    python main.py screen [wide|sp500|market]   — Print today's ranked pre-breakout list (market = whole US market)
    python main.py backtest [wide|sp500|market] — Run the walk-forward backtest and print metrics (market = whole US market)

Set your API keys in environment variables before running:
    export ETORO_API_KEY="your_public_key"
    export ETORO_USER_KEY="your_user_key"

Or edit config.py directly (not recommended for production).
"""

from __future__ import annotations

import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger(__name__)


def main():
    args    = sys.argv[1:]
    command = args[0] if args else "help"

    # Database-writing commands are serialized against the scheduler daemon via an
    # advisory file lock, so a manual run can never collide with a scheduled one
    # (the concurrent-writer race that can corrupt the SQLite file).
    if command in _WRITE_COMMANDS:
        from pipeline_lock import pipeline_lock, PipelineBusyError
        try:
            with pipeline_lock(label=command):
                _dispatch(command, args)
        except PipelineBusyError as exc:
            log.error("%s", exc)
            sys.exit(1)
    else:
        _dispatch(command, args)


# Commands that write to market_data.db / positions.json (must hold the lock).
_WRITE_COMMANDS = {
    "backfill", "refresh", "sync-positions", "export",
    "screener-backfill", "update-macro", "import-statement",
}


def _dispatch(command, args):
    if command == "backfill":
        from pipeline import backfill
        tickers = args[1:] or None
        backfill(tickers)

    elif command == "refresh":
        from pipeline import refresh
        tickers = args[1:] or None
        refresh(tickers)

    elif command == "sync-positions":
        from pipeline import sync_positions
        sync_positions()

    elif command == "account-summary":
        from account_summary import run_account_summary
        run_account_summary()

    elif command == "query":
        _cmd_query(args[1:])

    elif command == "summary":
        _cmd_summary()

    elif command == "scheduler":
        from scheduler import run_scheduler
        run_scheduler()

    elif command == "setup-sheets":
        from sheets_exporter import setup_sheets
        setup_sheets()

    elif command == "export":
        from sheets_exporter import run_export
        run_export(trigger="manual")

    elif command == "update-macro":
        from macro_cache import refresh_all_macro
        tickers = args[1:] or None
        refresh_all_macro(tickers)

    elif command == "screener-backfill":
        import screener as scr
        if "market" in args[1:]:
            # Whole liquid US market: fetch OHLCV, derive the liquid active set, cache sectors.
            result = scr.backfill_yfinance(scr.load_us_market(), chunk=100)
            active = scr.build_active_universe()
            scr.backfill_sectors(active)
            print(f"\nWhole-market backfill: {result['stored']:,} rows across "
                  f"{result['symbols']} symbols ({len(result['failed'])} failed); "
                  f"{len(active)} liquid names in the active universe.")
        else:
            tickers = [t.upper() for t in args[1:]] or scr.discovery_universe()
            result = scr.backfill_yfinance(tickers)
            print(f"\nyfinance backfill: {result['stored']:,} rows across "
                  f"{result['symbols']} symbols ({len(result['failed'])} failed).")

    elif command == "screen":
        from screener import screen_today
        screen_today(_screen_universe(args[1:]))

    elif command == "backtest":
        from screener import walk_forward_backtest
        walk_forward_backtest(_screen_universe(args[1:]))

    elif command == "import-statement":
        if len(args) < 2:
            print("Usage: python main.py import-statement <path_to_etoro_statement.csv>")
            print("Export from eToro → Portfolio → History → Account Statement → CSV")
            sys.exit(1)
        from statement_importer import import_etoro_statement
        import_etoro_statement(args[1])

    else:
        print(__doc__)
        sys.exit(0)


def _screen_universe(flags: list[str]):
    """Resolve the screener universe from CLI flags:
        'market' → whole liquid US market (∪ holdings), 'sp500' → S&P 500 (∪ holdings),
        'wide' → 45-name stress set, default → 24 large caps."""
    import screener as scr
    from config import SCREEN_UNIVERSE_WIDE
    if "market" in flags:
        return scr.discovery_universe()
    if "sp500" in flags:
        return sorted(set(scr.load_sp500()) | scr.held_tickers())
    if "wide" in flags:
        return SCREEN_UNIVERSE_WIDE
    return None


def _cmd_query(args: list[str]):
    """Print the last N candles for a ticker from the local cache."""
    if not args:
        print("Usage: python main.py query <TICKER> [days]")
        print("Example: python main.py query NVDA 30")
        sys.exit(1)

    ticker = args[0].upper()
    days   = int(args[1]) if len(args) > 1 else 10

    from database import get_candles, get_latest_close
    candles = get_candles(ticker, days=days)

    if not candles:
        print(f"No data found for {ticker}. Run: python main.py backfill {ticker}")
        sys.exit(1)

    latest = get_latest_close(ticker)
    print(f"\n{ticker} — last {len(candles)} trading days\n")
    print(f"{'Date':<12} {'Open':>8} {'High':>8} {'Low':>8} {'Close':>8} {'Volume':>12}")
    print("─" * 62)

    for prev, curr in zip([None] + candles, candles):
        chg_str = ""
        if prev:
            chg = (curr["close"] - prev["close"]) / prev["close"] * 100
            chg_str = f"  {'↑' if chg >= 0 else '↓'}{abs(chg):.2f}%"

        print(
            f"{curr['date']:<12} "
            f"{curr['open']:>8.2f} "
            f"{curr['high']:>8.2f} "
            f"{curr['low']:>8.2f} "
            f"{curr['close']:>8.2f} "
            f"{curr['volume']:>12,.0f}"
            f"{chg_str}"
        )

    print()


def _cmd_summary():
    """Print a one-line summary for every ticker in the cache."""
    from database import get_portfolio_summary

    rows = get_portfolio_summary()
    if not rows:
        print("No data in cache. Run: python main.py backfill")
        sys.exit(1)

    print(f"\n{'Ticker':<8} {'Date':<12} {'Open':>8} {'High':>8} {'Low':>8} {'Close':>8} {'Prev':>8} {'Chg%':>7}")
    print("─" * 75)

    for row in rows:
        chg      = row.get("day_change_pct") or 0
        arrow    = "↑" if chg >= 0 else "↓"
        chg_str  = f"{arrow}{abs(chg):.2f}%"

        print(
            f"{row['ticker']:<8} "
            f"{row.get('latest_date', 'N/A'):<12} "
            f"{row.get('latest_open', 0):>8.2f} "
            f"{row.get('latest_high', 0):>8.2f} "
            f"{row.get('latest_low', 0):>8.2f} "
            f"{row.get('latest_close', 0):>8.2f} "
            f"{row.get('prev_close', 0):>8.2f} "
            f"{chg_str:>7}"
        )

    print()


if __name__ == "__main__":
    main()
    