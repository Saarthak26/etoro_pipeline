"""
config.py — Central configuration for the eToro data pipeline.

Fill in your API keys from https://www.etoro.com/settings/trade
"""

import os

# ── API credentials ──────────────────────────────────────────────────────────
# Load from environment variables (recommended) or paste directly for testing.
ETORO_API_KEY  = os.getenv("ETORO_API_KEY",  "")
ETORO_USER_KEY = os.getenv("ETORO_USER_KEY", "")

BASE_URL = "https://public-api.etoro.com/api/v1"

# ── Your watchlist ────────────────────────────────────────────────────────────
# These are the tickers from your current portfolio plus any you want to track.
# The pipeline resolves each to an eToro instrumentId on first run and caches it.
WATCHLIST_TICKERS = [
    "NVDA", "AMD",  "ANET", "AMZN", "MU",
    "ASTS", "QBTS", "RKLB", "LASR", "BW",  "NVTS",
    "NBIS", "WMT",  "DVN",  "JPM",  "SHOP","MSFT","GOOG","FORM",
]

COMPANY_INFO = {
    "NVDA": "Nvidia — leading GPU and AI chip designer powering data centres, gaming, and autonomous vehicles.",
    "AMD":  "Advanced Micro Devices — high-performance CPUs and GPUs challenging Intel and Nvidia across PC, server, and gaming markets.",
    "ANET": "Arista Networks — cloud networking switches and EOS software serving hyperscale data centres and financial firms.",
    "AMZN": "Amazon — global e-commerce leader and operator of AWS, the world's largest cloud computing platform.",
    "MU":   "Micron Technology — major manufacturer of DRAM and NAND flash memory chips for PCs, servers, and mobile devices.",
    "ASTS": "AST SpaceMobile — building the first space-based broadband cellular network directly accessible by standard smartphones.",
    "QBTS": "D-Wave Quantum — commercial quantum computing systems and cloud services targeting optimisation problems.",
    "RKLB": "Rocket Lab — small-satellite launch provider and spacecraft manufacturer with the reusable Electron rocket.",
    "LASR": "nLIGHT — high-power programmable fibre lasers used in defence directed-energy weapons and industrial cutting.",
    "BW":   "Babcock & Wilcox — energy and environmental technology company focused on clean-energy and waste-to-energy solutions.",
    "NVTS": "Navitas Semiconductor — next-generation GaN and SiC power semiconductors enabling faster, smaller EV chargers and adapters.",
    "NBIS": "Nebius Group — AI-native cloud infrastructure company (spun out of Yandex) offering GPU clusters and MLOps tooling.",
    "WMT":  "Walmart — world's largest retailer with a rapidly growing e-commerce, advertising, and fintech ecosystem.",
    "DVN":  "Devon Energy — independent U.S. oil and gas exploration and production company focused on the Permian Basin.",
    "JPM":  "JPMorgan Chase — largest U.S. bank by assets and a global leader in investment banking, markets, and consumer finance.",
    "SHOP": "Shopify — e-commerce platform and merchant operating system powering millions of businesses in over 175 countries.",
    "MSFT": "Microsoft — enterprise software (Office, Windows), cloud (Azure), gaming (Xbox), and AI leader via OpenAI partnership.",
    "GOOG": "Alphabet/Google — dominant search and digital advertising platform with cloud (GCP), AI (Gemini), and autonomous-driving (Waymo) bets.",
    "FORM": "FormFactor — semiconductor wafer probe cards and advanced packaging test solutions used by leading chip foundries.",
}

# ── Data settings ─────────────────────────────────────────────────────────────
# How many daily candles to fetch on the initial backfill (max 1000 per eToro).
INITIAL_CANDLES_COUNT = 1000       # ~4 years of daily data on first run
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
REQUEST_DELAY_SECONDS = 1.0        # Pause between API calls
MAX_RETRIES           = 4          # Retry attempts on 429 or 5xx
BACKOFF_FACTOR        = 2          # Exponential backoff multiplier

# ── Portfolio baseline ────────────────────────────────────────────────────────
# Uninvested cash in your eToro account ("available cash" in the app), in USD.
# e.g. €83 × 1.13 EUR/USD ≈ $94. Added as a constant to every day's portfolio value.
INITIAL_CASH = 801.65

# ── Google Sheets ─────────────────────────────────────────────────────────────
# 1. Place your service account JSON key at the path below (or set the env var).
# 2. Run: python main.py setup-sheets  — creates the sheet and prints its ID.
# 3. Paste the printed Sheet ID into GOOGLE_SHEET_ID (or set the env var).
GOOGLE_SHEETS_CREDENTIALS_PATH = os.getenv(
    "GOOGLE_CREDENTIALS_PATH",
    os.path.join(os.path.dirname(__file__), "google_credentials.json"),
)
GOOGLE_SHEET_ID   = os.getenv("GOOGLE_SHEET_ID", "1AENJcGr49x1CG46BvUjMWPY36dSrLefkBE0jjBBrwx8")
GOOGLE_SHEET_NAME = "Trading Dashboard"                 # Used only when creating a new sheet
POSITIONS_PATH    = os.path.join(os.path.dirname(__file__), "positions.json")
MACRO_CACHE_PATH  = os.path.join(os.path.dirname(__file__), "macro_cache.json")

# Export schedule: NYSE/NASDAQ market open (09:30) and close (16:00) New York time.
SHEETS_OPEN_HOUR    = 9
SHEETS_OPEN_MINUTE  = 30
SHEETS_CLOSE_HOUR   = 16
SHEETS_CLOSE_MINUTE = 0
SHEETS_MARKET_TZ    = "America/New_York"

# ── Pre-breakout screener + walk-forward backtester ───────────────────────────
# All tunables for screener.py live here. See screener.py for the full logic.
#
# Data note: this pipeline stores OHLCV in SQLite (market_data.db), not BigQuery.
# The screener reuses database.get_connection(). The stored `close` is already
# split-adjusted (verified against NVDA 10:1 / AMZN 20:1), so we treat it as the
# adjusted-close series. There is no market-cap column, so the "market cap or
# dollar-volume floor" is implemented as a dollar-volume floor (SCREEN_MIN_DOLLAR_VOL).
SCREEN = {
    "HISTORY_DAYS":     400,      # Bars per symbol for today's `screen` ranking (recent state)
    "BACKTEST_HISTORY_DAYS": 2000,  # Bars per symbol for the walk-forward (use full history for a real multi-year test)
    "FORWARD_WINDOW":   60,       # Trading days looked ahead when building the training label
    "RALLY_THRESHOLD":  0.20,     # Label = 1 if max forward gain reaches +20%
    # ── Multi-horizon growth-window prediction ─────────────────────────────────
    # One classifier per horizon predicts "does the +RALLY_THRESHOLD move happen
    # within N trading days?". The predicted growth window is the earliest horizon
    # whose probability clears WINDOW_PROB_THRESHOLD. PRIMARY_HORIZON drives the
    # top-N pick ranking (kept near MAX_HOLD_DAYS so ranking and the trade sim agree).
    "HORIZONS":         [10, 30, 90, 180],  # Trading-day horizons for the timing models
    "PRIMARY_HORIZON":  90,       # Horizon whose score ranks the picks
    "WINDOW_PROB_THRESHOLD": 0.5, # Earliest horizon with P >= this = predicted window
    "REBALANCE_FREQ":   "W",      # Weekly rebalance (last real trading day of each ISO week)
    "TOP_N":            5,        # Positions opened per rebalance
    "STOP_LOSS":        0.10,     # 10% hard stop
    "TAKE_PROFIT":      0.20,     # 20% take profit
    "MAX_HOLD_DAYS":    90,       # Time exit
    "FEE_BPS":          10,       # Fee per side, basis points (10 bps = 0.10%)
    "SLIPPAGE_BPS":     10,       # Slippage per side, basis points
    "MIN_PRICE":        5.0,      # Price filter (drop sub-$5 names)
    "MIN_DOLLAR_VOL":   20_000_000,  # Liquidity floor: 20-day avg dollar volume, USD
    "WARMUP_DAYS":      252,      # Bars required before a symbol is scoreable (252d range)
    "RETRAIN_EVERY":    8,        # Walk-forward: retrain the scorer every N rebalances
                                  # (=1 retrains weekly; higher is faster on a big universe,
                                  #  still strict train-before-test — the model is only reused
                                  #  forward, never trained on future data)
    # ── Tilt / neutralization levers (compared in the walk-forward) ────────────
    "LABEL_MODE":       "triple_barrier",  # "triple_barrier" -> +TAKE_PROFIT hit before -STOP_LOSS
                                  #                  within the horizon (matches the backtest's exit
                                  #                  logic); best win rate + calibration in validation.
                                  # "fixed"       -> +RALLY_THRESHOLD max-gain in FORWARD_WINDOW days
                                  # "vol_adjusted"-> target scaled to the stock's own trailing
                                  #                  volatility, so calm & wild names are judged
                                  #                  on comparable moves (removes the vol/tech tilt)
    "VOL_TARGET_K":     2.5,      # vol_adjusted target = max(RALLY_THRESHOLD, K · σ_fwd),
                                  #   σ_fwd = trailing daily vol × sqrt(FORWARD_WINDOW)
    "MAX_PER_SECTOR":   None,     # None -> no cap (keep tilt); e.g. 2 -> ≤2 picks per sector

    # ── Probability calibration + data-noise reduction ─────────────────────────
    # Each flag was validated one-at-a-time in the walk-forward (win rate / expectancy
    # + per-horizon Brier / mean-pred-vs-actual). Defaults reflect what actually
    # improved out-of-sample. Headline finding: sample-weighting + triple-barrier
    # labels calibrate the probabilities better as a byproduct (90d pred/act 61/50 ->
    # 46/44) than the explicit isotonic/sigmoid calibrator, which overfits the
    # data-starved long horizons — so CALIBRATE defaults OFF.
    "CALIBRATE":            False,     # Explicit isotonic/sigmoid calibrator (overfits long horizons)
    "CALIBRATION_METHOD":   "isotonic",  # "isotonic" | "sigmoid"
    "CALIBRATION_HOLDOUT":  0.25,      # Fraction of (time-ordered) train reserved for the calibrator
    "CALIBRATION_MIN":      250,       # Skip calibration if the holdout has fewer rows than this
    "CLEAN_OHLCV":          True,      # Reject bad candles (bad OHLC, high<low, zero-vol, spikes) — kept
    "BAD_TICK_PCT":         0.5,       # Single-bar spike-and-revert threshold vs local median
    "SAMPLE_WEIGHTING":     True,      # Average-uniqueness weights for overlapping labels — clear win
    "CROSS_SECTIONAL_NORM": None,      # None | "rank" | "zscore" — neutral in validation, left off
}

# Broad discovery universe: the S&P 500 constituents, backfilled through the eToro
# pipeline into market_data.db. Loaded from this file (one ticker per line) so the
# screener can find NEW names you don't already own — not just your watchlist.
SP500_PATH = os.path.join(os.path.dirname(__file__), "sp500.txt")

# Whole liquid US market: NASDAQ+NYSE common stocks (nasdaqtrader.com listings),
# one Yahoo-form ticker per line. This is the broad discovery pool so the screener
# can find winners that are NOT in the S&P 500 (QBTS, SanDisk, ASTS, RKLB, …).
US_MARKET_PATH = os.path.join(os.path.dirname(__file__), "us_market.txt")
# Cache of the liquidity-filtered active scan set (derived from screener_candles).
ACTIVE_UNIVERSE_PATH = os.path.join(os.path.dirname(__file__), "active_universe.txt")

# Test universe: ~24 liquid US large caps. Names already backfilled in the DB are
# reused; the rest are pulled through the existing eToro pipeline (backfill).
SCREEN_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOG", "META", "AVGO", "TSLA",
    "JPM",  "WMT",  "ORCL", "COST", "HD",   "KO",   "JNJ",  "XOM",
    "PG",   "MA",   "V",    "UNH",  "AMD",  "CRM",  "NFLX", "BAC",
]

# Stress-test universe: the large caps above plus the higher-beta small/mid names
# already cached in the DB. Several of these (CRBU, LASR, NVTS, QBTS, IONQ, BW)
# stumbled badly, which reduces the survivorship tilt of the pure-large-cap set.
SCREEN_UNIVERSE_WIDE = SCREEN_UNIVERSE + [
    "ACET", "ANET", "ASML", "ASTS", "BW",   "CRBU", "DELL", "DVN",
    "FORM", "IONQ", "LASR", "MU",   "NBIS", "NVTS", "QBTS", "RKLB",
    "SHOP", "SNDK", "STX.US", "TEAM", "WDC",
]
