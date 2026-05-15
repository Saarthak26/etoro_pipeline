"""
config.py — Central configuration for the eToro data pipeline.

Fill in your API keys from https://www.etoro.com/settings/trade
"""

import os

# ── API credentials ──────────────────────────────────────────────────────────
# Load from environment variables (recommended) or paste directly for testing.
ETORO_API_KEY  = os.getenv("ETORO_API_KEY",  "sdgdskldFPLGfjHn1421dgnlxdGTbngdflg6290bRjslfihsjhSDsdgGHH25hjf")
ETORO_USER_KEY = os.getenv("ETORO_USER_KEY", "eyJjaSI6IjYwY2FiYjBiLTU1OTctNDQ4NS04ZjYzLTdlOWUwNTZlMGJiOCIsImVhbiI6IlVucmVnaXN0ZXJlZEFwcGxpY2F0aW9uIiwiZWsiOiJUNVlhcnFOUm5PdlNvem1qSWlRMjdYcWZNc2VobmcwRGZHZklleU5BN3M4clVFaGpadWlzY2pBUWhlZ0NkWU9RWUk2WGRzSm92MGN4MW1UQzltUEtBOWNiYlc4RGpVYW9ZU1RHYlA5YVE2a18ifQ__")

BASE_URL = "https://public-api.etoro.com/api/v1"

# ── Your watchlist ────────────────────────────────────────────────────────────
# These are the tickers from your current portfolio plus any you want to track.
# The pipeline resolves each to an eToro instrumentId on first run and caches it.
WATCHLIST_TICKERS = [
    "NVDA",   # Nvidia
    "AMD",    # Advanced Micro Devices
    "ANET",   # Arista Networks
    "AMZN",   # Amazon
    "MU",     # Micron Technology
    "ASTS",   # AST SpaceMobile
    "QBTS",   # D-Wave Quantum
    "RKLB",   # Rocket Lab
    "LASR",   # nLIGHT
    "BW",     # Babcock & Wilcox
    "NVTS",   # Navitas Semiconductor
]

# ── Data settings ─────────────────────────────────────────────────────────────
# How many daily candles to fetch on the initial backfill (max 1000 per eToro).
INITIAL_CANDLES_COUNT = 365        # ~1 year of daily data on first run
REFRESH_CANDLES_COUNT = 10         # Last 10 days on daily refresh (catches weekends and gaps)
CACHE_TTL_HOURS       = 12         # Re-fetch if the last pull was more than 12 hours ago

# Candle intervals available: OneMinute, FiveMinutes, TenMinutes, FifteenMinutes,
# ThirtyMinutes, OneHour, FourHours, OneDay, OneWeek
DAILY_INTERVAL   = "OneDay"
INTRADAY_INTERVAL = "OneHour"

# ── SQLite ────────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "market_data.db")

# ── Scheduler ─────────────────────────────────────────────────────────────────
# Runs the daily refresh job at 23:00 Berlin time (after US market close).
SCHEDULER_HOUR   = 23
SCHEDULER_MINUTE = 0
SCHEDULER_TZ     = "Europe/Berlin"

# ── Rate limiting ─────────────────────────────────────────────────────────────
# eToro allows 60 GET requests/min. We stay well under that.
REQUEST_DELAY_SECONDS = 1.5        # Pause between API calls
MAX_RETRIES           = 4          # Retry attempts on 429 or 5xx
BACKOFF_FACTOR        = 2          # Exponential backoff multiplier
