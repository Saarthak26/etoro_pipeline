"""
config.py — Central configuration for the eToro data pipeline.

Fill in your API keys from https://www.etoro.com/settings/trade
"""

import os

# ── API credentials ──────────────────────────────────────────────────────────
# Load from environment variables (recommended) or paste directly for testing.
ETORO_API_KEY  = os.getenv("ETORO_API_KEY",  "sdgdskldFPLGfjHn1421dgnlxdGTbngdflg6290bRjslfihsjhSDsdgGHH25hjf")
ETORO_USER_KEY = os.getenv("ETORO_USER_KEY", "eyJjaSI6IjYwY2FiYjBiLTU1OTctNDQ4NS04ZjYzLTdlOWUwNTZlMGJiOCIsImVhbiI6IlVucmVnaXN0ZXJlZEFwcGxpY2F0aW9uIiwiZWsiOiJZcVFuLkloLm9SWHA2NmNsZjBGSWl4VWNqRXk0dEdDczNnRXhmT08zbGl0akpYR0UtdGJDSXRaTktOWU94dWF4SGZsNFVtSmlocjBwaENrSzlNbE5nb1dkcGMwV0V1OHRJUTNkTmw3aXBlSV8ifQ__")

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

# Export schedule: NYSE/NASDAQ market open (09:30) and close (16:00) New York time.
SHEETS_OPEN_HOUR    = 9
SHEETS_OPEN_MINUTE  = 30
SHEETS_CLOSE_HOUR   = 16
SHEETS_CLOSE_MINUTE = 0
SHEETS_MARKET_TZ    = "America/New_York"
