# eToro Data Pipeline — Sprint 1

A lightweight, self-contained Python pipeline that pulls OHLCV market data
from the eToro API and caches it in a local SQLite database.

---

## What this does

- Resolves your watchlist tickers (NVDA, AMD, ASTS, etc.) to eToro instrument IDs
- Fetches up to 365 daily candles per ticker on the first run (backfill)
- Refreshes stale tickers each evening via an automated scheduler
- Stores everything in `market_data.db` (SQLite, no external DB needed)
- Respects eToro's rate limits with built-in throttling and exponential backoff

---

## Setup

**1. Get your API keys**

Go to https://www.etoro.com/settings/trade and generate your keys.

**2. Set environment variables**

```bash
export ETORO_API_KEY="your_public_key_here"
export ETORO_USER_KEY="your_user_key_here"
```

Or edit `config.py` directly for quick testing.

**3. Install dependencies**

```bash
pip install -r requirements.txt
```

---

## Commands

```bash
# First-time run — fetches ~1 year of daily data for all watchlist tickers
python main.py backfill

# Daily refresh — only re-fetches tickers whose cache is stale
python main.py refresh

# Refresh specific tickers only
python main.py refresh NVDA AMD ASTS

# Print the last 10 candles for a ticker
python main.py query NVDA

# Print last 30 candles
python main.py query NVDA 30

# Print the latest close + day change for your whole watchlist
python main.py summary

# Start the background scheduler (runs refresh every evening at 23:00 Berlin time)
python main.py scheduler
```

---

## File structure

```
etoro_pipeline/
├── config.py          ← API keys, watchlist, settings
├── etoro_client.py    ← Authenticated HTTP client with retry logic
├── database.py        ← SQLite schema and read/write helpers
├── pipeline.py        ← Orchestrator: backfill and refresh logic
├── scheduler.py       ← APScheduler daily job
├── main.py            ← CLI entry point
├── requirements.txt
└── market_data.db     ← Created automatically on first run
```

---

## Customising your watchlist

Edit `WATCHLIST_TICKERS` in `config.py`:

```python
WATCHLIST_TICKERS = [
    "NVDA", "AMD", "ASTS", "QBTS", "LASR",
    "BW", "NVTS", "RKLB", "MU", "AMZN", "ANET",
]
```

After adding new tickers, run `python main.py backfill` to fetch their history.

---

## Using the database in your own scripts

```python
from database import get_candles, get_latest_close, get_portfolio_summary

# Get last 30 daily candles for NVDA
candles = get_candles("NVDA", days=30)
for row in candles:
    print(row["date"], row["open"], row["close"])

# Get the latest close price
latest = get_latest_close("NVDA")
print(latest["date"], latest["close"])

# Get a summary of all tickers
summary = get_portfolio_summary()
for row in summary:
    print(row["ticker"], row["latest_close"], row["day_change_pct"])
```

---

## Rate limits

eToro allows 60 GET requests per minute. The pipeline inserts a 1.5-second
pause between API calls, so a full backfill of 11 tickers takes roughly
20 seconds. Daily refreshes are faster since they only update stale tickers.

---

## Next steps (Sprint 2)

Sprint 2 builds a Streamlit dashboard on top of this data layer — candlestick
charts, moving averages, RSI, and volume bars — all reading from `market_data.db`
without any additional API calls.
