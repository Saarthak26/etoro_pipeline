from __future__ import annotations

"""
screener.py — Pre-breakout stock screener + walk-forward backtester.

Built directly on the existing SQLite OHLCV cache (market_data.db) that the
eToro/yfinance pipeline populates. It reuses database.get_connection() rather
than opening a new data path.

Four parts, mirroring the spec:

  1. Feature engineering — compute_features(): pre-breakout state features from
     daily OHLCV. Every feature at date t uses only bars up to and including t
     (all indicators are causal / trailing), so there is no lookahead.

  2. Labelling — make_labels(): every symbol on every historical date is labelled
     with an honest forward outcome — 1 if the max gain over the next
     FORWARD_WINDOW trading days reaches RALLY_THRESHOLD. Winners and losers alike
     are kept. The label is used only for training and never enters the features.

  3. Scoring — PreBreakoutScorer: a gradient-boosted classifier (xgboost, with an
     sklearn HistGradientBoosting fallback) fit on the whole universe. The
     predicted probability of the positive class is the pre-breakout score.
     feature_importance() exposes what drives it.

  4. Walk-forward backtest — walk_forward_backtest(): on each weekly rebalance
     date the scorer is trained ONLY on samples whose full forward label window
     closed strictly before that date (embargo -> no leakage), the eligible
     universe is scored as of that date, liquidity/price/dollar-volume filters are
     applied, the top TOP_N names are taken, and each is simulated forward with a
     hard stop, take profit, and time exit, charging fees + slippage on entry and
     exit. Rebalance dates are snapped to real trading days in the data.

Entry points:
    screen_today()            — print today's ranked pre-breakout list
    walk_forward_backtest()   — run the full backtest and print the metrics
(both are wired into main.py as `screen` and `backtest`.)

Tunables live in config.SCREEN / config.SCREEN_UNIVERSE.
"""

import json
import logging

import numpy as np
import pandas as pd

from config import (
    SCREEN, SCREEN_UNIVERSE, SCREEN_UNIVERSE_WIDE, SP500_PATH, POSITIONS_PATH,
    US_MARKET_PATH, ACTIVE_UNIVERSE_PATH,
)
from database import get_connection

log = logging.getLogger(__name__)

FEATURE_COLS = [
    "pos_252",          # position within trailing 252-day range [0..1]
    "dist_below_high",  # fraction below the trailing 252-day high
    "tightness_20",     # 20-day high-low range as a fraction of price
    "ma_stack",         # 1 if close > MA20 > MA50 > MA200 else 0
    "px_vs_ma50",       # close / MA50 - 1
    "rsi",              # RSI(14) level
    "rsi_slope_5",      # 5-day slope of RSI
    "vol_contraction",  # 10-day avg volume / 50-day avg volume
    "obv_slope",        # normalised OBV slope (accumulation)
    "trend_63",         # 63-day price trend
]
# Auxiliary columns carried alongside the features for filtering / simulation,
# never fed to the model:
META_COLS = ["price", "avg_dollar_vol"]


# ── Universe helpers ──────────────────────────────────────────────────────────

# eToro uses suffixed/aliased symbols for a few names; map them to Yahoo Finance
# tickers so yfinance backfill and the screener agree on one symbol per company.
_YF_MAP = {"STX.US": "STX"}


def to_yahoo(ticker: str) -> str:
    """Normalise an eToro/watchlist ticker to its Yahoo Finance symbol.

    Screener OHLCV is stored under the Yahoo symbol, so the universe, holdings
    flag, and price data all key on the same string. Handles the '.US' suffix and
    the split-ticker dot→dash convention (BRK.B → BRK-B) yfinance expects.
    """
    t = (ticker or "").strip().upper()
    if t in _YF_MAP:
        return _YF_MAP[t]
    if t.endswith(".US"):
        t = t[:-3]
    return t.replace(".", "-") if "." in t else t


def load_sp500() -> list[str]:
    """Load the S&P 500 constituent list (Yahoo-form symbols, one per line)."""
    return _load_symbol_file(SP500_PATH)


def load_us_market() -> list[str]:
    """Load the whole liquid US market list (NASDAQ+NYSE common stocks, Yahoo-form)."""
    return _load_symbol_file(US_MARKET_PATH)


def load_active_universe() -> list[str]:
    """Load the cached liquidity-filtered active scan set, or fall back to the full
    market list if it hasn't been computed yet."""
    active = _load_symbol_file(ACTIVE_UNIVERSE_PATH)
    return active or load_us_market()


def _load_symbol_file(path: str) -> list[str]:
    try:
        with open(path) as f:
            return [t.strip().upper() for t in f if t.strip()]
    except FileNotFoundError:
        return []


def build_active_universe(min_price: float | None = None,
                          min_dollar_vol: float | None = None,
                          lookback: int = 20) -> list[str]:
    """
    Derive the liquid, tradeable subset of screener_candles and cache it to
    ACTIVE_UNIVERSE_PATH. A name is kept if its most recent `lookback`-day average
    price and dollar volume clear the floors — this is the "liquid" in "liquid US
    market" and keeps the ~6.9k backfilled names down to a tractable scan set.
    """
    mp = min_price if min_price is not None else SCREEN["MIN_PRICE"]
    mdv = min_dollar_vol if min_dollar_vol is not None else SCREEN["MIN_DOLLAR_VOL"]
    with get_connection() as conn:
        rows = conn.execute(
            """
            WITH recent AS (
                SELECT ticker, close, volume,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
                FROM screener_candles
            )
            SELECT ticker,
                   AVG(close)          AS px,
                   AVG(close * volume) AS dvol
            FROM recent
            WHERE rn <= ?
            GROUP BY ticker
            """,
            (lookback,),
        ).fetchall()

    active = sorted(
        r["ticker"] for r in rows
        if (r["px"] or 0) >= mp and (r["dvol"] or 0) >= mdv
    )
    with open(ACTIVE_UNIVERSE_PATH, "w") as f:
        f.write("\n".join(active))
    log.info("active universe: %d liquid names (of %d in screener_candles)",
             len(active), len(rows))
    return active


def held_tickers() -> set[str]:
    """Currently-held tickers (units > 0) from positions.json, as Yahoo symbols —
    used to flag which ranked names you already own vs. which are NEW ideas."""
    try:
        with open(POSITIONS_PATH) as f:
            return {to_yahoo(e["ticker"]) for e in json.load(f)
                    if float(e.get("units") or 0) > 0}
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return set()


def discovery_universe() -> list[str]:
    """
    Broad universe (Yahoo symbols) the screener scans to find NEW winners across the
    whole market: the liquidity-filtered active set ∪ S&P 500 ∪ the wide watchlist ∪
    current holdings. build_panel() self-filters any symbol lacking OHLCV in
    screener_candles, so un-backfilled names simply drop out. Falls back to S&P 500 +
    watchlist if the market hasn't been backfilled yet.
    """
    names = set(load_active_universe())
    names |= set(load_sp500())
    names |= {to_yahoo(t) for t in SCREEN_UNIVERSE_WIDE}
    names |= held_tickers()
    return sorted(names)


# ── Data loading / yfinance backfill (dedicated screener_candles table) ───────
#
# The screener reads its own table, screener_candles, populated from yfinance.
# This is deliberate: eToro's per-instrument daily candles do NOT share a common
# trading calendar across symbols, so a cross-sectional universe scan lands
# rebalance dates on days most names don't have — collapsing the backtest. yfinance
# returns every US ticker on one NYSE calendar and is split+dividend adjusted
# (auto_adjust=True), so `close` here is a true adjusted-close series. The eToro
# daily_candles table (used by the dashboard) is left untouched.

_SCREENER_SCHEMA = """
CREATE TABLE IF NOT EXISTS screener_candles (
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,      -- ISO date, Yahoo NYSE calendar
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,              -- split + dividend adjusted
    volume      REAL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_screener_candles_ticker_date
    ON screener_candles (ticker, date ASC);
"""


def _ensure_screener_table() -> None:
    with get_connection() as conn:
        conn.executescript(_SCREENER_SCHEMA)


def load_ohlcv(ticker: str, limit: int | None = None) -> pd.DataFrame:
    """
    Load a symbol's daily OHLCV from screener_candles (yfinance-sourced), oldest
    first. `ticker` is normalised to its Yahoo symbol. `close` is adjusted.

    Returns a DataFrame indexed by a DatetimeIndex with columns
    open/high/low/close/volume, or an empty frame if the symbol is not cached.
    """
    sym = to_yahoo(ticker)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT date, open, high, low, close, volume
            FROM   screener_candles
            WHERE  ticker = ?
            ORDER  BY date ASC
            """,
            (sym,),
        ).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"])
    if SCREEN.get("CLEAN_OHLCV"):
        df = _clean_ohlcv(df)
    if limit:
        df = df.tail(limit)
    return df


def _clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Drop obviously-bad candles so downstream features aren't computed on garbage:
    non-positive O/H/L/C, inconsistent bars (high<low, or high/low outside the
    open-close range), zero-volume days, and single-bar spike-and-revert ticks
    (close deviating > BAD_TICK_PCT from the centered 5-day median, which is robust
    to the spike itself — so sustained real moves are kept, isolated spikes dropped).
    """
    if df.empty:
        return df
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
    good = (o > 0) & (h > 0) & (l > 0) & (c > 0) & (v > 0)
    hi_oc = np.maximum(o, c)
    lo_oc = np.minimum(o, c)
    good &= (h >= l) & (h >= hi_oc) & (l <= lo_oc)
    df = df[good.fillna(False)]

    pct = float(SCREEN.get("BAD_TICK_PCT", 0.5) or 0)
    if pct > 0 and len(df) >= 5:
        med = df["close"].rolling(5, center=True, min_periods=3).median()
        dev = (df["close"] / med - 1).abs()
        df = df[~(dev > pct).fillna(False)]
    return df


def backfill_yfinance(tickers: list[str] | None = None,
                      period: str = "6y", chunk: int = 50) -> dict:
    """
    Backfill screener OHLCV from yfinance into screener_candles (adjusted, one
    NYSE calendar). Downloads in chunks to stay polite. Idempotent — re-running
    refreshes existing rows via INSERT OR REPLACE.

    Returns {"symbols", "stored", "failed"}.
    """
    import time as _time
    from datetime import datetime, timezone
    import yfinance as yf

    tickers = tickers or discovery_universe()
    ysyms = sorted({to_yahoo(t) for t in tickers})
    _ensure_screener_table()
    now = datetime.now(timezone.utc).isoformat()

    stored, failed = 0, []
    for start in range(0, len(ysyms), chunk):
        batch = ysyms[start:start + chunk]
        try:
            data = yf.download(batch, period=period, auto_adjust=True,
                               group_by="ticker", threads=True, progress=False)
        except Exception as exc:
            log.warning("yfinance batch download failed (%s): %s", batch[:3], exc)
            failed.extend(batch)
            continue

        rows = []
        for sym in batch:
            try:
                sub = data[sym] if len(batch) > 1 else data
            except Exception:
                failed.append(sym)
                continue
            sub = sub.dropna(how="all")
            if sub.empty:
                failed.append(sym)
                continue
            for idx, r in sub.iterrows():
                c = r.get("Close")
                if c is None or pd.isna(c):
                    continue
                def _n(x):
                    return None if (x is None or pd.isna(x)) else float(x)
                rows.append((sym, idx.strftime("%Y-%m-%d"),
                             _n(r.get("Open")), _n(r.get("High")), _n(r.get("Low")),
                             _n(c), _n(r.get("Volume")), now))

        if rows:
            with get_connection() as conn:
                conn.executemany(
                    """INSERT OR REPLACE INTO screener_candles
                       (ticker, date, open, high, low, close, volume, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
            stored += len(rows)
        log.info("yfinance backfill: %d/%d symbols, %d rows stored",
                 min(start + chunk, len(ysyms)), len(ysyms), stored)
        _time.sleep(1.0)

    log.info("yfinance backfill complete: %d symbols, %d rows, %d failed",
             len(ysyms), stored, len(failed))
    if failed:
        log.info("failed symbols: %s", failed)
    return {"symbols": len(ysyms), "stored": stored, "failed": failed}


# ── Sector metadata (for transparency + optional sector-neutral picks) ────────

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS screener_meta (
    ticker      TEXT PRIMARY KEY,
    sector      TEXT,
    industry    TEXT,
    fetched_at  TEXT
);
"""


def _ensure_meta_table() -> None:
    with get_connection() as conn:
        conn.executescript(_META_SCHEMA)


def backfill_sectors(tickers: list[str] | None = None, chunk: int = 200) -> int:
    """Populate screener_meta with sector/industry from yfinance for tickers we
    don't already have. Cached, so this only pays the (slow) lookup once per name."""
    from datetime import datetime, timezone
    import yfinance as yf

    _ensure_meta_table()
    tickers = [to_yahoo(t) for t in (tickers or load_active_universe())]
    with get_connection() as conn:
        have = {r["ticker"] for r in conn.execute("SELECT ticker FROM screener_meta")}
    todo = sorted(set(tickers) - have)
    if not todo:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for i in range(0, len(todo), chunk):
        batch = todo[i:i + chunk]
        rows = []
        try:
            tk = yf.Tickers(" ".join(batch))
            for sym in batch:
                info = {}
                try:
                    info = tk.tickers[sym].get_info() or {}
                except Exception:
                    pass
                rows.append((sym, info.get("sector") or "", info.get("industry") or "", now))
        except Exception as exc:
            log.warning("sector fetch batch failed: %s", exc)
            rows = [(sym, "", "", now) for sym in batch]
        with get_connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO screener_meta (ticker, sector, industry, fetched_at) VALUES (?, ?, ?, ?)",
                rows,
            )
        added += len(rows)
        log.info("sector backfill: %d/%d", min(i + chunk, len(todo)), len(todo))
    return added


def load_sectors(tickers: list[str] | None = None) -> dict[str, str]:
    """Return {ticker: sector} from screener_meta (blank sector -> 'Unknown')."""
    _ensure_meta_table()
    with get_connection() as conn:
        rows = conn.execute("SELECT ticker, sector FROM screener_meta").fetchall()
    m = {r["ticker"]: (r["sector"] or "Unknown") for r in rows}
    if tickers is not None:
        keep = {to_yahoo(t) for t in tickers}
        m = {k: v for k, v in m.items() if k in keep}
    return m


# ── 1. Feature engineering (causal, no lookahead) ─────────────────────────────

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. Causal: value at t uses only closes up to t."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100 - (100 / (1 + rs))


def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-balance volume. Causal cumulative sum of signed volume."""
    sign = np.sign(close.diff().fillna(0.0))
    return (sign * volume).cumsum()


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute pre-breakout state features for every date in df.

    All features are trailing/causal, so the row at date t depends only on bars
    up to and including t. No forward information enters here — labelling is done
    separately in make_labels().

    Returns a DataFrame indexed by date with FEATURE_COLS + META_COLS. Early rows
    without enough history are NaN and are dropped by the caller / model as needed.
    """
    if df.empty:
        return pd.DataFrame(columns=FEATURE_COLS + META_COLS)

    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    out = pd.DataFrame(index=df.index)

    # Trailing 252-day range position and distance below the high
    hi_252 = high.rolling(252, min_periods=60).max()
    lo_252 = low.rolling(252, min_periods=60).min()
    rng_252 = (hi_252 - lo_252).replace(0.0, np.nan)
    out["pos_252"] = (close - lo_252) / rng_252
    out["dist_below_high"] = (hi_252 - close) / hi_252

    # 20-day range tightness as a fraction of price (consolidation squeeze)
    hi_20 = high.rolling(20, min_periods=20).max()
    lo_20 = low.rolling(20, min_periods=20).min()
    out["tightness_20"] = (hi_20 - lo_20) / close

    # Moving-average stack and price vs the 50-day
    ma20 = close.rolling(20, min_periods=20).mean()
    ma50 = close.rolling(50, min_periods=50).mean()
    ma200 = close.rolling(200, min_periods=200).mean()
    out["ma_stack"] = (
        (close > ma20) & (ma20 > ma50) & (ma50 > ma200)
    ).astype(float)
    out["px_vs_ma50"] = close / ma50 - 1

    # RSI level and its 5-day slope
    rsi = _rsi(close, 14)
    out["rsi"] = rsi
    out["rsi_slope_5"] = (rsi - rsi.shift(5)) / 5.0

    # Volume contraction: 10-day avg volume / 50-day avg volume
    vol_ma10 = vol.rolling(10, min_periods=10).mean()
    vol_ma50 = vol.rolling(50, min_periods=50).mean()
    out["vol_contraction"] = vol_ma10 / vol_ma50.replace(0.0, np.nan)

    # OBV slope over 20 days, normalised by 50-day dollar-neutral volume scale
    obv = _obv(close, vol)
    obv_change = obv - obv.shift(20)
    out["obv_slope"] = obv_change / (vol_ma50.replace(0.0, np.nan) * 20.0)

    # 63-day (≈ one quarter) price trend
    out["trend_63"] = close / close.shift(63) - 1

    # Liquidity / price metadata (used for filtering, never fed to the model)
    out["price"] = close
    out["avg_dollar_vol"] = (close * vol).rolling(20, min_periods=20).mean()

    return out


# ── 2. Labelling (forward outcome, training-only) ─────────────────────────────

def make_labels(df: pd.DataFrame,
                forward_window: int | None = None,
                threshold: float | None = None,
                label_mode: str | None = None) -> pd.Series:
    """
    Honest forward label for every date: 1 if the max gain over the next
    `forward_window` trading days (future highs vs today's close) reaches the target,
    else 0. Winners and losers alike are labelled.

    LABEL_MODE:
      "fixed"        -> constant target = `threshold` (default +20%). This rewards big
                        absolute moves, so volatile sectors clear it far more often.
      "vol_adjusted" -> per-row target = max(threshold, K · σ_fwd), where σ_fwd is the
                        stock's trailing daily volatility scaled to the forward horizon.
                        Volatile names must move MORE than +20% to count, which removes
                        the structural volatility/tech tilt. The target never drops below
                        `threshold`, so it stays aligned with the fixed 20% take-profit.

    The last `forward_window` rows have an incomplete window -> NaN (dropped from
    training). This value is NEVER merged into the feature matrix.
    """
    fw = forward_window if forward_window is not None else SCREEN["FORWARD_WINDOW"]
    thr = threshold if threshold is not None else SCREEN["RALLY_THRESHOLD"]
    mode = label_mode if label_mode is not None else SCREEN.get("LABEL_MODE", "fixed")
    if df.empty:
        return pd.Series(dtype=float)

    close = df["close"]
    high = df["high"]
    n = len(df)
    labels = np.full(n, np.nan)
    hi = high.to_numpy()
    cl = close.to_numpy()

    if mode == "vol_adjusted":
        k = float(SCREEN.get("VOL_TARGET_K", 2.5))
        sigma_d = close.pct_change().rolling(60, min_periods=20).std().to_numpy()
        horizon = np.sqrt(fw)
        thr_row = np.maximum(thr, k * sigma_d * horizon)   # per-row target, floored at thr
    else:
        thr_row = np.full(n, thr)

    for i in range(n):
        j = i + fw
        if j >= n:
            break  # incomplete forward window -> leave NaN
        t = thr_row[i]
        if np.isnan(t):
            continue  # not enough vol history for a vol-adjusted target
        fwd_max = hi[i + 1: j + 1].max()
        labels[i] = 1.0 if (fwd_max / cl[i] - 1.0) >= t else 0.0
    return pd.Series(labels, index=df.index, name="label")


def horizon_label_col(h: int) -> str:
    """Column name for the forward label at horizon `h` trading days."""
    return f"label_h{h}"


def horizon_weight_col(h: int) -> str:
    """Column name for the sample weight at horizon `h` trading days."""
    return f"w_h{h}"


def make_triple_barrier_labels(df: pd.DataFrame, forward_window: int,
                               target: float | None = None,
                               stop: float | None = None):
    """
    Triple-barrier label matching the backtest's exit logic: 1 if +`target` is
    touched BEFORE −`stop` within `forward_window` bars, else 0 (stop-first or
    timeout). Stop assumed first on an ambiguous bar (conservative, like
    `simulate_trade`). Returns (labels Series, ends ndarray) where ends[i] is the
    index of the bar the outcome resolved on (barrier touch or window end) — used
    for uniqueness weighting. Incomplete windows -> NaN label.
    """
    target = target if target is not None else SCREEN["TAKE_PROFIT"]
    stop = stop if stop is not None else SCREEN["STOP_LOSS"]
    fw = forward_window
    n = len(df)
    cl, hi, lo = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    labels = np.full(n, np.nan)
    ends = np.minimum(np.arange(n) + fw, n - 1)
    for i in range(n):
        j = i + fw
        if j >= n:
            break  # incomplete forward window -> leave NaN
        up, dn = cl[i] * (1 + target), cl[i] * (1 - stop)
        outcome, end = 0.0, j
        for k in range(i + 1, j + 1):
            if lo[k] <= dn:            # stop first (conservative)
                outcome, end = 0.0, k
                break
            if hi[k] >= up:
                outcome, end = 1.0, k
                break
        labels[i], ends[i] = outcome, end
    return pd.Series(labels, index=df.index, name="label"), ends


def _avg_uniqueness_weights(n: int, ends: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """
    López de Prado average-uniqueness weights. Each label i "occupies" bars
    (i, ends[i]]; concurrency c_t = how many labels occupy bar t; the weight of
    label i is the mean of 1/c over its span. Down-weights heavily-overlapping
    (autocorrelated) labels. Fully vectorised. Invalid rows -> NaN.
    """
    diff = np.zeros(n + 2)
    idx = np.where(valid)[0]
    s = idx + 1                       # first occupied bar
    e = ends[idx]                     # last occupied bar
    ok = s <= e
    np.add.at(diff, s[ok], 1)
    np.add.at(diff, e[ok] + 1, -1)
    c = np.cumsum(diff[:n])
    with np.errstate(divide="ignore", invalid="ignore"):
        inv = np.where(c > 0, 1.0 / c, 0.0)
    prefix = np.concatenate([[0.0], np.cumsum(inv)])   # prefix[t] = sum inv[:t]
    w = np.full(n, np.nan)
    span = ends - np.arange(n)
    good = valid & (span > 0)
    gi = np.where(good)[0]
    w[gi] = (prefix[ends[gi] + 1] - prefix[gi + 1]) / span[gi]
    return w


def make_multi_labels(df: pd.DataFrame,
                      horizons: list[int] | None = None) -> pd.DataFrame:
    """
    Per horizon N: a forward label (`label_h{N}`) and a sample weight (`w_h{N}`).

    Label mode is `SCREEN["LABEL_MODE"]`: "fixed"/"vol_adjusted" use the forward-max
    rule (`make_labels`); "triple_barrier" uses the stop/target/time rule matching
    the backtest. Weights are average-uniqueness over each label's forward span,
    so overlapping (autocorrelated) labels are down-weighted. Longer horizons leave
    more recent rows NaN (window not yet closed).
    """
    horizons = horizons if horizons is not None else SCREEN["HORIZONS"]
    mode = SCREEN.get("LABEL_MODE", "fixed")
    n = len(df)
    out = {}
    for h in horizons:
        if mode == "triple_barrier":
            labels, ends = make_triple_barrier_labels(df, forward_window=h)
        else:
            labels = make_labels(df, forward_window=h)
            ends = np.minimum(np.arange(n) + h, n - 1)   # forward-max uses the full window
        out[horizon_label_col(h)] = labels
        valid = ~np.isnan(labels.to_numpy())
        out[horizon_weight_col(h)] = pd.Series(
            _avg_uniqueness_weights(n, ends, valid), index=df.index)
    return pd.DataFrame(out, index=df.index)


# ── Panel builder: features + labels + meta for a whole universe ──────────────

def build_panel(universe: list[str], history_days: int | None = None) -> pd.DataFrame:
    """
    Build one long-format panel across the universe with columns:
        ticker, date, <FEATURE_COLS>, <META_COLS>, label, <label_h{N} per horizon>

    The label columns are training targets only. `label` is kept as an alias of the
    primary-horizon label for backward compatibility. Rows are kept even when a
    label is NaN (recent dates) so they can still be scored today; training code
    drops NaNs per horizon.
    """
    hist = history_days if history_days is not None else SCREEN["HISTORY_DAYS"]
    primary_col = horizon_label_col(SCREEN["PRIMARY_HORIZON"])
    frames = []
    for ticker in universe:
        df = load_ohlcv(ticker, limit=hist)
        if df.empty or len(df) < SCREEN["WARMUP_DAYS"] // 4:
            log.debug("skip %s: %d bars", ticker, len(df))
            continue
        feats = compute_features(df)
        multi = make_multi_labels(df)
        for col in multi.columns:
            feats[col] = multi[col]
        feats["label"] = multi[primary_col]  # primary-horizon alias
        feats.insert(0, "ticker", ticker)
        feats = feats.reset_index().rename(columns={"index": "date"})
        frames.append(feats)

    label_cols = [horizon_label_col(h) for h in SCREEN["HORIZONS"]] + ["label"]
    if not frames:
        return pd.DataFrame(columns=["ticker", "date"] + FEATURE_COLS + META_COLS + label_cols)

    panel = pd.concat(frames, ignore_index=True)
    panel = panel.sort_values(["date", "ticker"]).reset_index(drop=True)
    panel = _cross_sectional_normalize(panel)
    return panel


# Continuous features normalized cross-sectionally; bounded/already-relative
# features (0/1 stack, 0–1 range position, 0–100 RSI) are left as-is.
_NORM_FEATURES = [c for c in FEATURE_COLS if c not in ("ma_stack", "pos_252", "rsi")]


def _cross_sectional_normalize(panel: pd.DataFrame) -> pd.DataFrame:
    """
    Per-date cross-sectional normalization of the continuous features, so a name's
    setup is judged relative to that day's universe rather than in absolute terms —
    stripping market-wide moves. Same-date only, so no lookahead. Controlled by
    SCREEN["CROSS_SECTIONAL_NORM"] ("rank" | "zscore" | None).
    """
    mode = SCREEN.get("CROSS_SECTIONAL_NORM")
    if not mode or panel.empty:
        return panel
    cols = [c for c in _NORM_FEATURES if c in panel.columns]
    g = panel.groupby("date")[cols]
    if mode == "rank":
        panel[cols] = g.rank(pct=True)
    elif mode == "zscore":
        mean = g.transform("mean")
        std = g.transform("std").replace(0.0, np.nan)
        panel[cols] = (panel[cols] - mean) / std
    return panel


# ── 3. Scoring (gradient-boosted classifier) ──────────────────────────────────

class PreBreakoutScorer:
    """
    Gradient-boosted classifier over FEATURE_COLS predicting the forward label.

    Prefers xgboost (as requested); falls back to sklearn HistGradientBoosting if
    xgboost cannot be imported/loaded in the environment. Either way, .score()
    returns the predicted probability of the positive (pre-breakout) class and
    .feature_importance() exposes the drivers.
    """

    def __init__(self, n_jobs: int = 4):
        self.model = None
        self.backend = None
        self._features = list(FEATURE_COLS)
        self.n_jobs = n_jobs

    def _make_model(self):
        try:
            from xgboost import XGBClassifier
            self.backend = "xgboost"
            return XGBClassifier(
                n_estimators=300,
                max_depth=3,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                min_child_weight=5,
                eval_metric="logloss",
                tree_method="hist",
                n_jobs=self.n_jobs,
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            from sklearn.ensemble import HistGradientBoostingClassifier
            log.warning("xgboost unavailable (%s); using sklearn HistGradientBoosting", exc)
            self.backend = "sklearn"
            return HistGradientBoostingClassifier(
                max_iter=300, max_depth=3, learning_rate=0.05,
                l2_regularization=1.0,
            )

    def fit(self, train: pd.DataFrame, label_col: str = "label",
            weight_col: str | None = None) -> "PreBreakoutScorer":
        """Fit on rows with a defined label. `train` must contain FEATURE_COLS +
        `label_col` (+ `date` for calibration, + `weight_col` if sample-weighting).
        `label_col` lets one scorer class serve any forward horizon."""
        self._calibrator = None
        data = train.dropna(subset=self._features + [label_col])
        y = data[label_col].to_numpy(dtype=int)
        self.model = self._make_model()
        # Degenerate case (single class in the training window): fall back to a
        # constant base rate so the walk-forward never crashes early in history.
        if len(np.unique(y)) < 2:
            self.model = None
            self._base_rate = float(y.mean()) if len(y) else 0.0
            self._n_train = len(y)
            return self

        sw = (data[weight_col].to_numpy(dtype=float)
              if (weight_col and weight_col in data.columns) else None)

        # ── Optional probability calibration on a time-ordered holdout ──────────
        # Older rows train the model; the newest CALIBRATION_HOLDOUT fraction fits
        # the calibrator — strictly within the training window, so no lookahead.
        if SCREEN.get("CALIBRATE") and "date" in data.columns:
            data = data.sort_values("date")
            X_all = data[self._features].to_numpy(dtype=float)
            y_all = data[label_col].to_numpy(dtype=int)
            sw_all = (data[weight_col].to_numpy(dtype=float)
                      if (weight_col and weight_col in data.columns) else None)
            n_cal = int(len(data) * float(SCREEN.get("CALIBRATION_HOLDOUT", 0.25)))
            cut = len(data) - n_cal
            yb, yc = y_all[:cut], y_all[cut:]
            if (n_cal >= int(SCREEN.get("CALIBRATION_MIN", 250))
                    and len(np.unique(yb)) == 2 and len(np.unique(yc)) == 2):
                self.model.fit(X_all[:cut], yb,
                               sample_weight=sw_all[:cut] if sw_all is not None else None)
                raw_c = self.model.predict_proba(X_all[cut:])[:, 1]
                self._fit_calibrator(raw_c, yc)
                self._base_rate = float(y_all.mean())
                self._n_train = cut
                return self

        # No calibration (disabled, too little data, or single-class holdout).
        X = data[self._features].to_numpy(dtype=float)
        y = data[label_col].to_numpy(dtype=int)
        self.model.fit(X, y, sample_weight=sw)
        self._base_rate = float(y.mean())
        self._n_train = len(y)
        return self

    def _fit_calibrator(self, raw: np.ndarray, y: np.ndarray) -> None:
        """Fit a 1-D calibrator mapping raw P(positive) -> calibrated probability."""
        method = SCREEN.get("CALIBRATION_METHOD", "isotonic")
        if method == "sigmoid":
            from sklearn.linear_model import LogisticRegression
            cal = LogisticRegression()
            cal.fit(raw.reshape(-1, 1), y)
            self._calibrator = ("sigmoid", cal)
        else:
            from sklearn.isotonic import IsotonicRegression
            cal = IsotonicRegression(out_of_bounds="clip")
            cal.fit(raw, y)
            self._calibrator = ("isotonic", cal)

    def _apply_calibrator(self, raw: np.ndarray) -> np.ndarray:
        kind, cal = self._calibrator
        if kind == "sigmoid":
            return cal.predict_proba(raw.reshape(-1, 1))[:, 1]
        return cal.predict(raw)

    def score(self, feats: pd.DataFrame) -> np.ndarray:
        """Return P(positive) for each row of `feats` (NaN features -> base rate)."""
        X = feats[self._features].to_numpy(dtype=float)
        if self.model is None:
            return np.full(len(feats), getattr(self, "_base_rate", 0.0))
        raw = self.model.predict_proba(np.nan_to_num(X, nan=np.nan))[:, 1]
        if getattr(self, "_calibrator", None) is not None:
            return self._apply_calibrator(raw)
        return raw

    def feature_importance(self) -> pd.Series:
        if self.model is None:
            return pd.Series(0.0, index=self._features)
        if self.backend == "xgboost":
            imp = self.model.feature_importances_
        else:
            # sklearn HistGradientBoosting has no native importances; use a
            # permutation-free proxy of 0 and rely on xgboost in practice.
            imp = getattr(self.model, "feature_importances_", np.zeros(len(self._features)))
        return pd.Series(imp, index=self._features).sort_values(ascending=False)


class MultiHorizonScorer:
    """
    One PreBreakoutScorer per forward horizon. Each sub-model predicts whether the
    +RALLY_THRESHOLD move happens within its horizon, trained on that horizon's own
    matured rows (longer horizons mature slower -> fewer rows; handled by dropna in
    PreBreakoutScorer.fit). Exposes:
      * .score(feats)          -> {horizon: P(move within horizon)} arrays
      * .score_frame(feats)    -> DataFrame with p_h{N} columns (+ the row index)
      * .primary_score(feats)  -> P at PRIMARY_HORIZON (drives ranking)
      * .predict_window(probs) -> earliest horizon with P >= threshold, else None
    """

    def __init__(self, horizons: list[int] | None = None,
                 primary: int | None = None,
                 threshold: float | None = None):
        self.horizons  = sorted(horizons if horizons is not None else SCREEN["HORIZONS"])
        self.primary   = primary if primary is not None else SCREEN["PRIMARY_HORIZON"]
        self.threshold = threshold if threshold is not None else SCREEN["WINDOW_PROB_THRESHOLD"]
        self.models: dict[int, PreBreakoutScorer] = {}

    def fit(self, train: pd.DataFrame) -> "MultiHorizonScorer":
        # The horizons are independent models, so train them concurrently. xgboost's
        # fit releases the GIL, so threads give near-linear speedup; we split the core
        # budget across horizons (rather than each grabbing all cores) to avoid
        # oversubscription. Falls back to sequential if anything goes wrong.
        import os
        from concurrent.futures import ThreadPoolExecutor

        n_h = max(1, len(self.horizons))
        per_model_jobs = max(1, (os.cpu_count() or 4) // n_h)

        use_weights = SCREEN.get("SAMPLE_WEIGHTING")

        def _fit_one(h: int):
            scorer = PreBreakoutScorer(n_jobs=per_model_jobs)
            wcol = horizon_weight_col(h) if use_weights else None
            return h, scorer.fit(train, label_col=horizon_label_col(h), weight_col=wcol)

        try:
            with ThreadPoolExecutor(max_workers=n_h) as ex:
                for h, model in ex.map(_fit_one, self.horizons):
                    self.models[h] = model
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Parallel horizon fit failed (%s); falling back to sequential", exc)
            self.models = {}
            for h in self.horizons:
                _, self.models[h] = _fit_one(h)
        return self

    @property
    def backend(self) -> str | None:
        m = self.models.get(self.primary)
        return m.backend if m else None

    def score(self, feats: pd.DataFrame) -> dict[int, np.ndarray]:
        return {h: m.score(feats) for h, m in self.models.items()}

    def score_frame(self, feats: pd.DataFrame) -> pd.DataFrame:
        cols = {f"p_h{h}": s for h, s in self.score(feats).items()}
        return pd.DataFrame(cols, index=feats.index)

    def primary_score(self, feats: pd.DataFrame) -> np.ndarray:
        return self.models[self.primary].score(feats)

    def feature_importance(self) -> pd.Series:
        return self.models[self.primary].feature_importance()

    def predict_window(self, probs: dict[int, float]) -> int | None:
        """Earliest horizon whose probability clears the threshold, else None."""
        for h in self.horizons:  # ascending
            if probs.get(h, 0.0) >= self.threshold:
                return h
        return None

    def window_series(self, score_frame: pd.DataFrame) -> pd.Series:
        """Map a per-horizon probability frame to the predicted window per row."""
        def _row_window(row) -> float:
            for h in self.horizons:
                if row.get(f"p_h{h}", 0.0) >= self.threshold:
                    return float(h)
            return float("nan")  # no horizon clears the bar
        return score_frame.apply(_row_window, axis=1)


# ── Rebalance calendar: snap to REAL trading days ─────────────────────────────

def rebalance_dates(all_dates: pd.DatetimeIndex, freq: str | None = None) -> list[pd.Timestamp]:
    """
    Return the last actual trading day of each period (weekly by default).

    Grouping real trading dates by period and taking the max in each bucket
    guarantees every rebalance date exists in the data — resample boundaries land
    on weekends/holidays and would otherwise yield zero trades.
    """
    freq = freq or SCREEN["REBALANCE_FREQ"]
    s = pd.Series(all_dates, index=all_dates)
    grouped = s.groupby(s.index.to_period(freq)).max()
    return sorted(grouped.tolist())


# ── 4a. Single-trade simulation with stop / target / time exit ────────────────

def simulate_trade(df: pd.DataFrame, entry_date: pd.Timestamp) -> dict | None:
    """
    Simulate one long trade opened at the close of `entry_date`.

    Exits at the first of: 10% hard stop, 20% take profit, or the 90-day time
    exit. Fees and slippage are charged on both entry and exit. If the stop and
    target are both touched on the same bar, the stop is assumed first
    (conservative). Returns a trade dict, or None if there is no post-entry data.
    """
    stop = SCREEN["STOP_LOSS"]
    target = SCREEN["TAKE_PROFIT"]
    max_hold = SCREEN["MAX_HOLD_DAYS"]
    fee = SCREEN["FEE_BPS"] / 10_000.0
    slip = SCREEN["SLIPPAGE_BPS"] / 10_000.0

    idx = df.index
    if entry_date not in idx:
        return None
    pos = idx.get_loc(entry_date)
    fut = df.iloc[pos + 1: pos + 1 + max_hold]
    if fut.empty:
        return None

    entry_close = float(df["close"].iloc[pos])
    entry_fill = entry_close * (1 + slip)          # buy slippage
    stop_px = entry_close * (1 - stop)
    target_px = entry_close * (1 + target)

    exit_fill = None
    exit_date = None
    outcome = None
    for d, row in fut.iterrows():
        if row["low"] <= stop_px:                  # stop first (conservative)
            exit_fill = stop_px * (1 - slip)
            exit_date, outcome = d, "stop"
            break
        if row["high"] >= target_px:
            exit_fill = target_px * (1 - slip)
            exit_date, outcome = d, "target"
            break
    if exit_fill is None:                          # time exit at last bar's close
        last = fut.iloc[-1]
        exit_fill = float(last["close"]) * (1 - slip)
        exit_date, outcome = fut.index[-1], "time"

    # Net return: slippage baked into fills, fee charged per side.
    net_ret = (exit_fill / entry_fill) - 1 - 2 * fee
    hold_days = int(idx.get_loc(exit_date) - pos)
    return {
        "entry_date": entry_date,
        "exit_date": exit_date,
        "entry_price": entry_close,
        "exit_price": exit_fill,
        "outcome": outcome,
        "hold_days": hold_days,
        "return": net_ret,
    }


def time_to_target(df: pd.DataFrame, entry_date: pd.Timestamp,
                   threshold: float | None = None,
                   max_h: int | None = None) -> int | None:
    """
    Ground-truth timing: trading days from `entry_date` until the first forward bar
    whose high reaches +threshold vs the entry close, censored at `max_h` days.
    Ignores stops — this measures WHEN the target would have been hit, independent
    of the trade sim's exit logic, so it can validate the horizon predictions.
    Returns the day count, or None if the target is not reached within `max_h`.
    """
    thr = threshold if threshold is not None else SCREEN["RALLY_THRESHOLD"]
    max_h = max_h if max_h is not None else max(SCREEN["HORIZONS"])
    idx = df.index
    if entry_date not in idx:
        return None
    pos = idx.get_loc(entry_date)
    fut = df.iloc[pos + 1: pos + 1 + max_h]
    if fut.empty:
        return None
    entry_close = float(df["close"].iloc[pos])
    target_px = entry_close * (1 + thr)
    hits = np.where(fut["high"].to_numpy() >= target_px)[0]
    return int(hits[0]) + 1 if len(hits) else None


# ── Eligibility filter ────────────────────────────────────────────────────────

def _eligible(rows: pd.DataFrame) -> pd.DataFrame:
    """Apply price, dollar-volume floor and complete-feature filters."""
    ok = rows.dropna(subset=FEATURE_COLS + META_COLS)
    ok = ok[(ok["price"] >= SCREEN["MIN_PRICE"]) &
            (ok["avg_dollar_vol"] >= SCREEN["MIN_DOLLAR_VOL"])]
    return ok


def _pick_top_n(elig: pd.DataFrame, n: int,
                sectors: dict | None = None,
                max_per_sector: int | None = None) -> pd.DataFrame:
    """Top-n eligible names by score, optionally capping picks per sector. Names with
    an unknown sector are exempt from the cap (each treated as its own bucket)."""
    ranked = elig.sort_values("score", ascending=False)
    if not max_per_sector or not sectors:
        return ranked.head(n)
    picks, counts = [], {}
    for _, row in ranked.iterrows():
        sec = sectors.get(row["ticker"], "Unknown")
        if sec != "Unknown" and counts.get(sec, 0) >= max_per_sector:
            continue
        picks.append(row)
        counts[sec] = counts.get(sec, 0) + 1
        if len(picks) >= n:
            break
    return pd.DataFrame(picks)


# ── 4b. Walk-forward backtest ─────────────────────────────────────────────────

def walk_forward_backtest(universe: list[str] | None = None,
                          verbose: bool = True) -> dict:
    """
    Rigorous weekly walk-forward:
      * rebalance dates snapped to real trading days,
      * scorer retrained at each date on samples whose full forward label window
        closed strictly BEFORE that date (embargo -> strict train-before-test,
        no label leakage),
      * top-N eligible names simulated forward with stop/target/time + fees+slippage.

    Returns {"trades": DataFrame, "metrics": dict}.
    """
    universe = universe or SCREEN_UNIVERSE
    horizons = sorted(SCREEN["HORIZONS"])
    max_h = max(horizons)

    panel = build_panel(universe, history_days=SCREEN["BACKTEST_HISTORY_DAYS"])
    if panel.empty:
        log.warning("No data in panel; nothing to backtest.")
        return {"trades": pd.DataFrame(), "metrics": {}}

    # Per-symbol OHLCV frames for the trade simulation (kept once, reused).
    price_frames = {t: load_ohlcv(t) for t in universe}

    all_dates = pd.DatetimeIndex(sorted(panel["date"].unique()))
    rebals = rebalance_dates(all_dates)
    # Need enough labelled history to train, and forward room to simulate.
    trades: list[dict] = []
    # Embargo sized to the LONGEST horizon so no horizon's label window overlaps the
    # scoring date — strict train-before-test for every sub-model (≈ max_h trading
    # days in calendar terms).
    embargo = pd.Timedelta(days=int(max_h * 1.6))

    retrain_every = max(1, int(SCREEN.get("RETRAIN_EVERY", 1)))
    max_per_sector = SCREEN.get("MAX_PER_SECTOR")
    sec_map = load_sectors()   # {ticker: sector}; empty -> everything "Unknown"
    primary_col = horizon_label_col(SCREEN["PRIMARY_HORIZON"])
    scorer = None
    since_train = 0
    scored_dates = 0
    for d in rebals:
        train = panel[panel["date"] <= (d - embargo)]
        if train.dropna(subset=[primary_col]).shape[0] < 300:
            continue  # not enough realised history yet

        # Retrain every `retrain_every` active rebalances. The model is only ever
        # reused FORWARD (scoring dates strictly after its training cutoff), so this
        # preserves strict train-before-test — it just avoids refitting weekly on a
        # large universe.
        if scorer is None or since_train >= retrain_every:
            scorer = MultiHorizonScorer().fit(train)
            since_train = 0
        since_train += 1

        today_rows = panel[panel["date"] == d]
        elig = _eligible(today_rows)
        if elig.empty:
            continue
        elig = elig.copy()
        probs = scorer.score_frame(elig)
        for col in probs.columns:
            elig[col] = probs[col].to_numpy()
        elig["window"] = scorer.window_series(probs).to_numpy()
        elig["score"] = scorer.primary_score(elig)
        picks = _pick_top_n(elig, SCREEN["TOP_N"], sec_map, max_per_sector)
        scored_dates += 1

        for _, row in picks.iterrows():
            pf = price_frames.get(row["ticker"])
            if pf is None or d not in pf.index:
                continue
            tr = simulate_trade(pf, d)
            if tr is None:
                continue
            tr["ticker"] = row["ticker"]
            tr["score"] = float(row["score"])
            tr["sector"] = sec_map.get(row["ticker"], "Unknown")
            # Timing: predicted window + ground-truth days-to-target (censored).
            tr["pred_window"] = float(row["window"]) if row["window"] == row["window"] else np.nan
            tr["time_to_target"] = time_to_target(pf, d, max_h=max_h)
            for h in horizons:
                tr[f"p_h{h}"] = float(row.get(f"p_h{h}", np.nan))
            trades.append(tr)

    trades_df = pd.DataFrame(trades)
    metrics = _aggregate(trades_df)
    metrics["timing"] = _aggregate_timing(trades_df, horizons)
    metrics["horizons"] = horizons
    metrics["primary_horizon"] = SCREEN["PRIMARY_HORIZON"]
    metrics["rebalance_dates"] = len(rebals)
    metrics["scored_dates"] = scored_dates
    metrics["universe_size"] = len([t for t in universe if not price_frames[t].empty])

    if verbose:
        _print_backtest(trades_df, metrics, universe)
    return {"trades": trades_df, "metrics": metrics}


# ── Metrics aggregation ───────────────────────────────────────────────────────

def _aggregate(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"n_trades": 0}

    r = trades["return"]
    wins = r[r > 0]
    losses = r[r <= 0]
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = losses.mean() if len(losses) else 0.0

    # Equity curve for drawdown: fixed-fractional, 1/TOP_N of a constant capital
    # base per trade, accrued in exit-date order. This avoids the nonsense of
    # compounding up to TOP_N concurrently-open (overlapping) trades as if each
    # reinvested 100% of capital. It is a portfolio proxy, not a full daily
    # mark-to-market, and is labelled as such in the output.
    n_slots = SCREEN["TOP_N"]
    ordered = trades.sort_values("exit_date")
    equity = 1 + (ordered["return"] / n_slots).cumsum()
    running_max = equity.cummax()
    max_dd = float((equity / running_max - 1).min()) if len(equity) else 0.0

    # Per-year breakdown keyed by entry year. `pnl_1_over_n` is the same
    # fixed-fractional contribution summed within the year (comparable across years).
    by_year = {}
    yr = trades["entry_date"].dt.year
    for y, grp in trades.groupby(yr):
        gr = grp["return"]
        by_year[int(y)] = {
            "n": int(len(grp)),
            "win_rate": float((gr > 0).mean()),
            "avg_return": float(gr.mean()),
            "pnl_1_over_n": float(gr.sum() / n_slots),
        }

    sector_mix = (trades["sector"].value_counts().to_dict()
                  if "sector" in trades.columns else {})

    return {
        "n_trades": int(len(trades)),
        "win_rate": float((r > 0).mean()),
        "avg_return": float(r.mean()),
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "reward_to_risk": float(avg_win / abs(avg_loss)) if avg_loss else float("nan"),
        "max_drawdown": max_dd,
        "avg_hold_days": float(trades["hold_days"].mean()),
        "exit_mix": trades["outcome"].value_counts().to_dict(),
        "sector_mix": sector_mix,
        "n_sectors": int(len([s for s in sector_mix if s != "Unknown"])),
        "by_year": by_year,
    }


def _aggregate_timing(trades: pd.DataFrame, horizons: list[int]) -> dict:
    """
    Validate the growth-window predictions against ground truth.

    * `by_window`: for each predicted-window bucket, how many picks, what fraction
      actually reached +RALLY_THRESHOLD within that window, and the median actual
      days-to-target among those that hit at all. This is the direct test of "when
      we say ~30 days, does it hit in ~30 days?".
    * `calibration`: per horizon, mean predicted probability vs the realised hit
      rate within that horizon, plus a Brier score (lower is better). This shows
      whether each classifier's probabilities are trustworthy.
    """
    if trades.empty or "pred_window" not in trades.columns:
        return {"by_window": {}, "calibration": {}, "n": 0}

    trades = trades.copy()
    # None (target never hit within max horizon) -> NaN so comparisons stay numeric.
    trades["time_to_target"] = pd.to_numeric(trades["time_to_target"], errors="coerce")
    tt = trades["time_to_target"]

    by_window = {}
    # One bucket per horizon, plus a ">max" bucket for picks that cleared no horizon.
    buckets = [(f"~{h}d", h) for h in horizons] + [(f">{max(horizons)}d", None)]
    for label, h in buckets:
        grp = trades[trades["pred_window"] == h] if h is not None \
            else trades[trades["pred_window"].isna()]
        if grp.empty:
            continue
        g_tt = grp["time_to_target"]
        hit = g_tt.notna()
        # "hit within the predicted window" — for the no-window bucket, credit any
        # eventual hit within the max horizon.
        within = (g_tt <= h) if h is not None else hit
        by_window[label] = {
            "n": int(len(grp)),
            "hit_within_window": float(within.mean()),
            "ever_hit": float(hit.mean()),
            "median_days_to_target": float(g_tt[hit].median()) if hit.any() else None,
        }

    calibration = {}
    for h in horizons:
        col = f"p_h{h}"
        if col not in trades.columns:
            continue
        p = trades[col]
        actual = (tt <= h).astype(float)          # realised hit within horizon h
        valid = p.notna()
        if not valid.any():
            continue
        calibration[h] = {
            "n": int(valid.sum()),
            "mean_pred": float(p[valid].mean()),
            "actual_rate": float(actual[valid].mean()),
            "brier": float(((p[valid] - actual[valid]) ** 2).mean()),
        }

    return {"by_window": by_window, "calibration": calibration, "n": int(len(trades))}


# ── Entry point A: today's ranked pre-breakout list ───────────────────────────

def rank_universe(universe: list[str] | None = None) -> dict:
    """
    Train on all available labelled history and score each symbol at its latest
    bar. Pure computation, no printing — used by screen_today() and by the Google
    Sheets exporter.

    Returns {"ranked": DataFrame (eligible, score-sorted), "importances": Series,
             "backend": str, "n_train": int, "asof": date|None}.
    """
    universe = universe or SCREEN_UNIVERSE
    panel = build_panel(universe)
    if panel.empty:
        return {"ranked": pd.DataFrame(), "importances": pd.Series(dtype=float),
                "backend": None, "n_train": 0, "asof": None}

    # Each horizon's classifier trains on its own matured rows (PreBreakoutScorer.fit
    # drops NaN labels per horizon). `train` here is the primary-horizon matured set,
    # used only for the sector base-rate diagnostic below.
    train = panel.dropna(subset=["label"])
    scorer = MultiHorizonScorer().fit(panel)

    # Latest available row per ticker (its 'today' state).
    latest = panel.sort_values("date").groupby("ticker", as_index=False).tail(1)
    elig = _eligible(latest).copy()
    if not elig.empty:
        probs = scorer.score_frame(elig)          # p_h10, p_h30, p_h90, p_h180
        for col in probs.columns:
            elig[col] = probs[col].to_numpy()
        elig["window"] = scorer.window_series(probs).to_numpy()
        elig["score"] = scorer.primary_score(elig)   # P(primary horizon) ranks picks
        elig = elig.sort_values("score", ascending=False).reset_index(drop=True)

    # Per-sector label base rate (the "why is it tech" validation): how often each
    # sector's setups actually reached the target historically. Reuses the panel.
    sec_map = load_sectors()
    base_rates = {}
    if sec_map and not train.empty:
        tr = train.assign(sector=train["ticker"].map(lambda t: sec_map.get(t, "Unknown")))
        g = tr.groupby("sector")["label"].agg(["mean", "count"])
        base_rates = {s: {"rate": float(r["mean"]), "n": int(r["count"])}
                      for s, r in g.sort_values("mean", ascending=False).iterrows()}

    primary_model = scorer.models.get(scorer.primary)
    return {
        "ranked": elig,
        "importances": scorer.feature_importance(),
        "backend": scorer.backend,
        "n_train": int(getattr(primary_model, "_n_train", 0)),
        "asof": elig["date"].max().date() if not elig.empty else None,
        "sector_base_rates": base_rates,
        "horizons": scorer.horizons,
        "primary_horizon": scorer.primary,
    }


def screen_today(universe: list[str] | None = None, top_n: int | None = None) -> pd.DataFrame:
    """
    Train on all available labelled history, score each symbol at its latest bar,
    apply the eligibility filters, and print the ranked pre-breakout list plus the
    model's feature importances. Returns the ranked DataFrame.
    """
    top_n = top_n or SCREEN["TOP_N"]

    res = rank_universe(universe)
    ranked = res["ranked"]
    if ranked.empty:
        print("No eligible names — backfill the universe or check the price/liquidity filters.")
        return pd.DataFrame()

    asof = res["asof"]
    horizons = res.get("horizons", SCREEN["HORIZONS"])
    primary = res.get("primary_horizon", SCREEN["PRIMARY_HORIZON"])
    print(f"\n── Pre-breakout ranking as of {asof} "
          f"(model: {res['backend']}, trained on {res['n_train']:,} samples, "
          f"score = P(+{SCREEN['RALLY_THRESHOLD']*100:.0f}% within {primary}d)) ──\n")
    hcols = "".join(f"{'P'+str(h)+'d':>7}" for h in horizons)
    print(f"{'#':>2}  {'Ticker':<7} {'Score':>7} {'Window':>8} {'Price':>9} "
          f"{'RSI':>6} {'$Vol(M)':>9}{hcols}")
    print("─" * (63 + 7 * len(horizons)))
    for i, row in ranked.head(top_n).iterrows():
        win = row.get("window")
        win_s = f"~{int(win)}d" if win == win and win is not None else ">180d"
        hvals = "".join(f"{row.get('p_h'+str(h), float('nan')):>7.2f}" for h in horizons)
        print(f"{i+1:>2}  {row['ticker']:<7} {row['score']:>7.3f} {win_s:>8} "
              f"{row['price']:>9.2f} {row['rsi']:>6.1f} "
              f"{row['avg_dollar_vol']/1e6:>9.1f}{hvals}")

    imp = res["importances"]
    print("\nTop feature importances:")
    for name, val in imp.head(6).items():
        print(f"   {name:<16} {val:.3f}")
    print()
    return ranked


# ── Pretty-print the backtest ─────────────────────────────────────────────────

def _print_backtest(trades: pd.DataFrame, m: dict, universe: list[str]) -> None:
    print(f"\n── Walk-forward backtest ── universe={m.get('universe_size')} names, "
          f"weekly, top-{SCREEN['TOP_N']} ──")
    print(f"   rebalance dates: {m.get('rebalance_dates')}  |  "
          f"dates actually traded: {m.get('scored_dates')}")
    if m.get("n_trades", 0) == 0:
        print("   No trades generated.\n")
        return

    print(f"\n   Trades           : {m['n_trades']}")
    print(f"   Win rate         : {m['win_rate']*100:5.1f}%")
    print(f"   Avg return/trade : {m['avg_return']*100:+5.2f}%")
    print(f"   Avg win          : {m['avg_win']*100:+5.2f}%")
    print(f"   Avg loss         : {m['avg_loss']*100:+5.2f}%")
    print(f"   Reward:risk      : {m['reward_to_risk']:.2f}")
    print(f"   Max drawdown     : {m['max_drawdown']*100:5.1f}%  (fixed-fractional 1/{SCREEN['TOP_N']}, exit-ordered)")
    print(f"   Avg hold days    : {m['avg_hold_days']:.1f}")
    print(f"   Exit mix         : {m['exit_mix']}")
    if m.get("sector_mix"):
        topsec = sorted(m["sector_mix"].items(), key=lambda x: -x[1])[:6]
        print(f"   Sectors of picks : {m.get('n_sectors', 0)} distinct  |  "
              + ", ".join(f"{k} {v}" for k, v in topsec))

    print(f"\n   {'Year':<6} {'Trades':>7} {'WinRate':>8} {'AvgRet':>8} {'PnL(1/N)':>10}")
    print("   " + "─" * 44)
    for y in sorted(m["by_year"]):
        s = m["by_year"][y]
        print(f"   {y:<6} {s['n']:>7} {s['win_rate']*100:>7.1f}% "
              f"{s['avg_return']*100:>+7.2f}% {s['pnl_1_over_n']*100:>+9.1f}%")

    _print_timing(m.get("timing", {}))
    print()


def _print_timing(timing: dict) -> None:
    """Reliability of the growth-window predictions vs. ground-truth days-to-target."""
    if not timing or not timing.get("by_window"):
        return
    thr = SCREEN["RALLY_THRESHOLD"] * 100
    print(f"\n   ── Timing reliability (did +{thr:.0f}% land in the predicted window?) ──")
    print(f"   {'PredWindow':<11} {'Picks':>6} {'HitInWin':>9} {'EverHit':>8} {'MedDays':>8}")
    print("   " + "─" * 46)
    for label, s in timing["by_window"].items():
        med = f"{s['median_days_to_target']:.0f}" if s["median_days_to_target"] is not None else "—"
        print(f"   {label:<11} {s['n']:>6} {s['hit_within_window']*100:>8.1f}% "
              f"{s['ever_hit']*100:>7.1f}% {med:>8}")

    cal = timing.get("calibration", {})
    if cal:
        print(f"\n   {'Horizon':<9} {'N':>6} {'MeanPred':>9} {'Actual':>8} {'Brier':>7}")
        print("   " + "─" * 42)
        for h in sorted(cal):
            c = cal[h]
            print(f"   {'~'+str(h)+'d':<9} {c['n']:>6} {c['mean_pred']*100:>8.1f}% "
                  f"{c['actual_rate']*100:>7.1f}% {c['brier']:>7.3f}")
