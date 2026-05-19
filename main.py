"""
main.py — Entry point for the eToro data pipeline.

Commands:
    python main.py backfill              — First-time full history fetch for all tickers
    python main.py refresh               — Incremental daily refresh (stale tickers only)
    python main.py refresh NVDA AMD      — Refresh specific tickers only
    python main.py query NVDA            — Print last 10 candles for a ticker
    python main.py summary               — Print the latest close for every ticker
    python main.py scheduler             — Start the background daily refresh daemon
    python main.py setup-sheets          — One-time Google Sheets setup wizard
    python main.py export                — Manually push all data to Google Sheets now

Set your API keys in environment variables before running:
    export ETORO_API_KEY="your_public_key"
    export ETORO_USER_KEY="your_user_key"

Or edit config.py directly (not recommended for production).
"""

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

    if command == "backfill":
        from pipeline import backfill
        tickers = args[1:] or None
        backfill(tickers)

    elif command == "refresh":
        from pipeline import refresh
        tickers = args[1:] or None
        refresh(tickers)

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

    else:
        print(__doc__)
        sys.exit(0)


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
    