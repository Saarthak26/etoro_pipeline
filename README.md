# eToro Trading Dashboard — Technical Reference & Changelog

> This document is the living technical reference for every metric, signal, and methodology
> used in this pipeline. Every time a logic changes, a new entry is added to the
> [Changelog](#changelog) at the bottom. Read top-to-bottom for a full understanding; jump
> straight to the changelog to see what changed in a given sprint.

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Data Pipeline](#2-data-pipeline)
3. [Technical Indicators](#3-technical-indicators)
4. [Signal Scoring System](#4-signal-scoring-system)
   - 4.1 [Legacy Fixed-Weight Path](#41-legacy-fixed-weight-path-logbook)
   - 4.2 [IC-Weighted Adaptive Path](#42-ic-weighted-adaptive-path-overview--per-ticker-tabs)
   - 4.3 [Regime Detection](#43-regime-detection)
   - 4.4 [Regime Multipliers](#44-regime-multipliers)
   - 4.5 [Fundamentals Overlay](#45-fundamentals-overlay)
   - 4.6 [Macro Adjustments](#46-macro-adjustments)
   - 4.7 [Signal Labels](#47-signal-labels)
5. [Portfolio Risk Metrics](#5-portfolio-risk-metrics)
   - 5.1 [Annualised Volatility (σ)](#51-annualised-volatility-σ)
   - 5.2 [Value at Risk 95% (VaR)](#52-value-at-risk-95-var)
   - 5.3 [Sortino Ratio](#53-sortino-ratio)
   - 5.4 [Portfolio Beta + HHI Composite](#54-portfolio-beta--hhi-composite)
   - 5.5 [Maximum Drawdown](#55-maximum-drawdown)
6. [Portfolio Performance Reconstruction](#6-portfolio-performance-reconstruction)
7. [SPY Benchmark & Alpha](#7-spy-benchmark--alpha)
8. [Sector Allocation](#8-sector-allocation)
9. [Average Hold Time](#9-average-hold-time)
10. [Sheet Tab Reference](#10-sheet-tab-reference)
11. [Scheduler](#11-scheduler)
12. [Pre-Breakout Screener (Multi-Horizon)](#12-pre-breakout-screener-multi-horizon)
13. [Changelog](#changelog)

---

## 1. System Architecture

```
eToro REST API
      │
      ▼
pipeline.py  ──►  SQLite (market_data.db)
                        │
                        ▼
              sheets_exporter.py
                        │
         ┌──────────────┼──────────────┐
         ▼              ▼              ▼
   Google Sheets    yfinance       positions.json
   (all tabs)   (fundamentals,   (open positions,
                 SPY, earnings)   entry prices)
```

**Key files:**

| File | Purpose |
|------|---------|
| `config.py` | API keys, watchlist, DB path, scheduler times, macro-cache path |
| `database.py` | SQLite schema, upsert/query helpers |
| `etoro_client.py` | eToro API wrapper (candles, instruments, portfolio) |
| `pipeline.py` | `backfill()`, `refresh()`, `sync_positions()` |
| `macro_cache.py` | Daily yfinance cache: news, analyst target, sector, 52-week range, short ratio, institutional % |
| `account_summary.py` | Read-only broker-authoritative account snapshot (equity, P&L, exposure) |
| `statement_importer.py` | One-off importer for eToro account-statement CSV (populates Closed Trades) |
| `sheets_exporter.py` | All indicator, signal, risk, dashboard, Looker and export logic |
| `screener.py` | Machine-learning pre-breakout screener — feature engineering, multi-horizon labels, gradient-boosted scorer, walk-forward backtest (see [Section 12](#12-pre-breakout-screener-multi-horizon)) |
| `scheduler.py` | APScheduler daemon — 5 jobs (see [Section 11](#11-scheduler)) |
| `main.py` | CLI entry point |

---

## 2. Data Pipeline

### Candle data (SQLite)

- Source: eToro public API (`/api/v1/instruments/{id}/candles`)
- Interval stored: `OneDay` (daily OHLCV)
- `INITIAL_CANDLES_COUNT = 1000` — fetched on first backfill (~4 years of history back to ~2022)
- `REFRESH_CANDLES_COUNT = 10` — fetched on daily refresh (catches weekends and gaps)
- Cache TTL: 12 hours — tickers are only re-fetched if stale

### Positions (`positions.json`)

- Source of truth for open positions: ticker, direction (BUY/SELL), units, entry price, open date, stop loss, take profit
- Synced from eToro portfolio API via `python main.py sync-positions`
- Also editable directly in the **Positions** sheet tab (synced back to `positions.json` at the start of every export)
- `open_date` format stored as `D-M-YYYY` (e.g. `14-4-2026`); internally converted to `YYYY-MM-DD` for comparisons

### Effective ticker list

Every export run builds the effective ticker list as:
```
WATCHLIST_TICKERS (from config.py)
  + any ticker found in positions.json with units > 0
  (deduped, order preserved)
```
Tickers discovered from positions that have no DB candle data are automatically backfilled before the export proceeds.

---

## 3. Technical Indicators

All indicators are computed from the last 250 daily candles using the `ta` library.

| Indicator | Window | Library | Description |
|-----------|--------|---------|-------------|
| **RSI** | 14 days | `ta.momentum.RSIIndicator` | Relative Strength Index. Oscillates 0–100. >70 = overbought, <30 = oversold |
| **MACD** | 12/26/9 EMA | `ta.trend.MACD` | Moving Average Convergence Divergence. Three values: MACD line, Signal line, Histogram (MACD − Signal) |
| **EMA 20** | 20 days | `ta.trend.EMAIndicator` | Short-term exponential moving average |
| **EMA 50** | 50 days | `ta.trend.EMAIndicator` | Mid-term exponential moving average |
| **EMA 200** | 200 days | `ta.trend.EMAIndicator` | Long-term trend baseline |
| **Bollinger Bands** | 20 days, 2σ | `ta.volatility.BollingerBands` | Upper/Middle/Lower bands. Price outside bands signals overextension |
| **ATR** | 14 days | `ta.volatility.AverageTrueRange` | Average True Range — measures daily price volatility |
| **Fisher Transform** | 9 days | Custom (via `ta`) | Converts price to Gaussian distribution. Crossover of Fisher vs its 1-day lag signals cycle turns |
| **Volume Ratio** | 20-day SMA | Custom | Today's volume ÷ 20-day average volume. >1.5 = high conviction, <0.7 = thin market |
| **ROC 5d** | 5 days | `pd.pct_change(5)` | 5-day rate of change — short-term price momentum |
| **ROC 20d** | 20 days | `pd.pct_change(20)` | 20-day rate of change — medium-term momentum |

### Trend classification

Derived from EMA alignment, applied to each ticker's latest candle:

| Condition | Label |
|-----------|-------|
| Close > EMA20 > EMA50 > EMA200 | Strong uptrend |
| Close > EMA50 > EMA200 | Uptrend |
| Close < EMA20 < EMA50 < EMA200 | Strong downtrend |
| Close < EMA50 < EMA200 | Downtrend |
| Otherwise | Neutral |

---

## 4. Signal Scoring System

Each ticker receives a **composite signal score** which drives the `Signal Label` column (Strong Buy / Buy / Hold / Sell / Strong Sell) in the Overview and per-ticker tabs.

Two paths exist depending on whether IC-based weights are available:

---

### 4.1 Legacy Fixed-Weight Path (Logbook)

Used when there is insufficient candle history to compute IC weights (< 40 candles), or in the Log Book tab where per-row historical computation is too expensive.

**Scoring table:**

| Signal | Condition | Points |
|--------|-----------|--------|
| EMA alignment | Strong uptrend | +2.0 |
| EMA alignment | Uptrend | +1.0 |
| EMA alignment | Downtrend | -1.0 |
| EMA alignment | Strong downtrend | -2.0 |
| RSI | ≥ 70 (overbought) | -1.0 |
| RSI | ≥ 60 (bullish momentum) | +0.5 |
| RSI | ≤ 30 (oversold, contrarian) | +1.0 |
| RSI | ≤ 40 (weakening) | -0.5 |
| MACD line | Above signal line | +1.0 |
| MACD line | Below signal line | -1.0 |
| MACD histogram | Positive | +0.25 |
| MACD histogram | Negative | -0.25 |
| Fisher Transform | Fisher > Fisher Signal (1-lag) | +0.5 |
| Fisher Transform | Fisher < Fisher Signal | -0.5 |
| Bollinger Bands | Price above upper band | -0.5 |
| Bollinger Bands | Price below lower band | +0.5 |
| Bollinger Bands | Price in upper half | +0.25 |
| Bollinger Bands | Price in lower half | -0.25 |
| Revenue growth | > 15% YoY | +0.25 |
| Revenue growth | Negative | -0.25 |
| Debt/Equity | D/E > 2.0 | -0.25 |

**Volume multiplier** (applied last):
```
multiplier = clamp(volume_ratio, 0.75, 1.5)
score = score × multiplier
```
High volume amplifies the signal (up to 1.5×); low volume dampens it (minimum 0.75×).

**Why this approach:** Provides a transparent, interpretable baseline with domain knowledge baked in. No historical data required per signal.

---

### 4.2 IC-Weighted Adaptive Path (Overview + Per-Ticker Tabs)

Used in the Overview and individual ticker tabs. Replaces static weights with data-driven weights computed from recent price history.

**IC = Information Coefficient**: Pearson correlation between a signal's daily value and the 5-day forward return. A high positive IC means the signal has genuinely predicted future direction in this ticker's recent history.

**Step 1 — Compute raw IC per signal (rolling 90-day window):**

```python
forward_return[t] = (close[t+5] - close[t]) / close[t]

for each signal in [rsi, macd_hist, ema_score, fisher, bb_position, roc_5, roc_20]:
    IC[signal] = Pearson_corr(signal_series[-90:], forward_return[-90:])
    # Requires >= 15 valid observations; else IC = 0
```

**Step 2 — Normalise so Σ|IC| = 1:**

```python
weight[signal] = IC[signal] / Σ|IC[all signals]|
```

A **negative IC** is allowed and kept as-is — it means the signal is a contrarian predictor (e.g. high RSI predicts a down move in a mean-reverting stock).

**Step 3 — Apply regime multipliers** (see [Section 4.4](#44-regime-multipliers)).

**Step 4 — Compute directional score per signal:**

Each signal is converted into a directional value in [-1, +1]:

| Signal | Rule |
|--------|------|
| RSI | ≥70 → -1.0 / ≥60 → +0.5 / ≤30 → +1.0 / ≤40 → -0.5 / else → 0 |
| MACD histogram | sign(hist): positive → +1, negative → -1 |
| EMA score | (close − EMA50) / EMA50 × 10, clamped to [-1, +1] |
| Fisher | Fisher > Fisher_Signal → +1.0, else → -1.0 |
| BB position | (close − BB_lower) / (BB_upper − BB_lower) − 0.5, then × 2, clamped |
| ROC 5d | sign: positive → +1, negative → -1 |
| ROC 20d | sign: positive → +1, negative → -1 |

**Step 5 — Aggregate:**

```python
raw_score = Σ(direction[signal] × weight[signal])   # in [-1, +1]
score     = raw_score × 5.0                          # rescaled to [-5, +5]
```

Fundamentals are then added on top (see [Section 4.5](#45-fundamentals-overlay)).

**Why IC weighting:** Static weights assume RSI or MACD always matter equally for every stock. In reality, a momentum stock like NVDA is better predicted by EMA/MACD; a mean-reverting stock like JPM is better predicted by RSI/BB. IC weights let the data tell us which signals have actually been predictive for this specific ticker over the last 90 days and weight them accordingly. This is the same approach used in JP Morgan's quantitative research on cross-asset signal combination.

---

### 4.3 Regime Detection

Before weights are applied, the market regime is classified using two inputs:

1. **SPY vs 200-day EMA**: Is the S&P 500 in a broad uptrend?
2. **Portfolio 20-day annualised realised volatility**: How turbulent is the portfolio recently?

```python
spy_above_200ma = SPY_close[-1] > EWM(SPY_close, span=200)[-1]
realised_vol    = std(portfolio_returns[-20:]) × √252

if spy_above_200ma AND realised_vol < 0.25:
    regime = "TRENDING"       # Broad market uptrend, low volatility
elif realised_vol > 0.40:
    regime = "HIGH_VOL"       # Elevated volatility regardless of trend
else:
    regime = "RANGING"        # Neither clearly trending nor high-vol
```

**Why this approach:** Markets cycle through environments where different signals dominate. Goldman Sachs quantitative strategy research ("Factor Timing in Equity Markets") demonstrates that momentum signals (EMA, MACD, ROC) outperform in trending regimes, while mean-reversion signals (RSI, Bollinger Bands) outperform when volatility is elevated. This two-factor classifier captures the dominant dynamics without overfitting.

---

### 4.4 Regime Multipliers

After IC weights are computed, regime-specific multipliers are applied and the weights are re-normalised:

| Signal | TRENDING | HIGH_VOL | RANGING |
|--------|----------|----------|---------|
| RSI | 0.6× | 1.5× | 1.0× |
| BB Position | 0.6× | 1.5× | 1.0× |
| MACD Histogram | 1.5× | 0.6× | 1.0× |
| EMA Score | 1.5× | 0.6× | 1.0× |
| Fisher | 1.2× | 0.8× | 1.0× |
| ROC 5d | 1.3× | 0.7× | 1.0× |
| ROC 20d | 1.3× | 0.7× | 1.0× |

**Effect:** In a TRENDING regime, momentum signals (MACD, EMA, ROC) carry more weight. In HIGH_VOL, mean-reversion signals (RSI, BB) carry more weight. RANGING is neutral — IC weights are used as-is.

---

### 4.5 Fundamentals Overlay

Added on top of the IC-weighted technical score (not IC-weighted themselves — fundamentals change quarterly, not daily, so there is insufficient frequency to compute a reliable IC):

| Condition | Adjustment |
|-----------|-----------|
| Revenue growth YoY > 15% | +0.25 |
| Revenue growth YoY < 0% | -0.25 |
| Debt/Equity > 2.0 | -0.25 |
| Earnings within 5 days | Score × 0.7 (30% dampening) |

The earnings penalty reflects uncertainty ahead of a binary event — the signal is less reliable when a large move is imminent regardless of technicals.

---

### 4.6 Macro Adjustments

Added on top of the technical score (both paths) using cached macro data from `macro_cache.py` (yfinance, refreshed daily). The component is surfaced as the **Macro Adj** column in the Overview tab and as a `MacroAdj:±X.XX` suffix in the signal reason string.

**Analyst-target gap** — distance of current close from the mean 12-month analyst price target:

| Gap = (close − target) / target | Macro Adj |
|---|---|
| > +40% | −0.75 (well above target, overvalued) |
| > +20% | −0.40 |
| > +10% | −0.20 |
| < −30% | +0.75 (deep discount to target) |
| < −15% | +0.40 |
| < −5% | +0.20 |

**52-week range proximity**:

| Condition | Macro Adj |
|---|---|
| Within 3% of 52-week high | −0.25 (extension risk) |
| Within 10% of 52-week low | +0.25 (mean-reversion candidate) |

**Why outside Fundamentals Overlay:** Analyst targets and 52-week extremes change daily with price, not quarterly with earnings, so they fit naturally as a price-driven overlay distinct from accounting fundamentals.

---

### 4.7 Signal Labels

Final score → label mapping (same thresholds for both paths):

| Score | Label |
|-------|-------|
| ≥ +3.5 | Strong Buy |
| +1.5 to +3.5 | Buy |
| -1.5 to +1.5 | Hold |
| -3.5 to -1.5 | Sell |
| ≤ -3.5 | Strong Sell |

---

## 5. Portfolio Risk Metrics

All risk metrics are computed from the **actual reconstructed daily portfolio return series** (not per-ticker theoretical distributions). The portfolio value on each day is:

```
portfolio_value[date] = Σ(ticker_close[date] × ticker_units)
                         for all active position legs open on that date
```

Daily returns:
```
r[t] = (value[t] - value[t-1]) / value[t-1]
```

A rolling window of up to **252 trading days** (1 year) is used. Minimum 20 returns required for any metric to be computed.

---

### 5.1 Annualised Volatility (σ)

**Formula:**
```
σ_daily  = std(daily_returns)
σ_annual = σ_daily × √252
```

**Score (1–10):**
```
score = clamp(σ_annual / 0.06, 1, 10)
```
Calibration: σ = 6% → score 1 (very low), σ = 60% → score 10 (very high).

| Score | Label |
|-------|-------|
| ≥ 8 | Very High |
| ≥ 6 | High |
| ≥ 4 | Medium |
| < 4 | Low |

**What it means:** How much the portfolio's daily value fluctuates. A 40% annualised vol means a typical daily swing of 40% / √252 ≈ ±2.5% is normal.

**Known limitation:** Portfolio returns are reconstructed using current units, not units held at the time. When new positions are opened, the portfolio value steps up on that date, which can create artificial spikes in the return series and inflate measured volatility.

---

### 5.2 Value at Risk 95% (VaR)

**Method:** Historical simulation (non-parametric). No distributional assumptions.

**Formula:**
```
VaR_pct    = 5th percentile of daily_returns
VaR_dollar = |VaR_pct| × current_portfolio_value
```

**Interpretation:** On the worst 1-in-20 trading days (5% of days), the portfolio loses at least VaR_dollar. Example: VaR = -3.2% / -$4,200 means on a bad day (5% probability), you'd lose $4,200 or more.

**Why historical simulation:** No assumption of normality. Equity returns have fat tails — a parametric (Gaussian) VaR would systematically understate tail losses. Historical simulation captures actual observed extreme moves.

---

### 5.3 Sortino Ratio

**Formula:**
```
downside_returns   = [r for r in daily_returns if r < 0]
downside_deviation = std(downside_returns) × √252    # annualised
annual_return      = (1 + mean(daily_returns))^252 - 1
sortino            = annual_return / downside_deviation
```

Risk-free rate is set to **0%** (simplest assumption; avoids requiring a live rate feed).

**Interpretation:**

| Sortino | Interpretation |
|---------|---------------|
| < 0 | Portfolio is losing money |
| 0–1 | Below-average risk-adjusted return |
| 1–2 | Good |
| > 2 | Excellent |

**Why Sortino over Sharpe:** Sharpe penalises upside volatility the same as downside volatility, which is not how investors experience risk. Sortino only penalises downside deviation, making it a more intuitive measure for a growth-oriented portfolio where large up-days are desirable.

---

### 5.4 Portfolio Beta + HHI Composite

A single **1–10 risk score** combining two independent risk dimensions:

#### Portfolio Beta

```
portfolio_beta = Σ(position_value × ticker_beta) / total_portfolio_value
```

`ticker_beta` sourced from yfinance (`info["beta"]`). Default = 1.0 if unavailable.

Beta > 1 means the portfolio amplifies market moves (high market sensitivity).

**Beta component of composite score:**
```
beta_score = portfolio_beta × 4.5
# beta 1.0 → 4.5, beta 2.0 → 9.0
```

#### HHI (Herfindahl-Hirschman Index) — Concentration

```
HHI = Σ((position_value_i / total_value)²)
```

Standard academic and regulatory concentration measure:
- 20 equal positions → HHI ≈ 0.05 (very diversified)
- 5 equal positions → HHI ≈ 0.20 (moderate concentration)
- 1 position → HHI = 1.0 (fully concentrated)

**Concentration component:**
```
conc_score = HHI × 3.0
```

#### Composite

```
composite_score = clamp(beta_score + conc_score, 1, 10)
```

| Score | Label |
|-------|-------|
| ≥ 8 | Very High |
| ≥ 6 | High |
| ≥ 4 | Medium |
| < 4 | Low |

**Why HHI over threshold-based:** A threshold system (e.g. "+1.5 if any position > 20% of portfolio") creates cliff effects — 19.9% and 20.1% concentration look identical but score differently. HHI is continuous and additive, rewarding gradual diversification proportionally. It is the standard measure used by the US DOJ for market concentration analysis and by institutional risk teams.

---

### 5.5 Maximum Drawdown

**Formula:**
```
peak = portfolio_history[0].value
for each day in portfolio_history:
    peak = max(peak, day.value)
    drawdown = (peak - day.value) / peak
    if drawdown > max_drawdown:
        max_drawdown = drawdown
        max_drawdown_date = day.date
```

**Displayed as:** `-38.1% (2026-01-14)` — the trough date, not the peak date.

**Interpretation:** The largest peak-to-trough decline the portfolio has experienced in the reconstructed history. Measures how painful a sustained losing streak has been.

---

## 6. Portfolio Performance Reconstruction

### `_build_portfolio_history(positions, from_date, to_date)`

Reconstructs the daily portfolio value for any date range using local SQLite candle data.

**Algorithm:**
1. For each open position leg (ticker + units):
   - Parse `open_date` from `D-M-YYYY` to `YYYY-MM-DD`
   - Fetch candles from `max(from_date, open_date)` to `to_date` using `get_candles_range()`
   - For each candle date, add `close_price × units` to that date's running total
2. Return sorted list of `{date, value}` oldest → newest

**Key design decisions:**
- Legs contribute starting from their actual open date — prevents phantom returns from "holding" positions before they were opened
- No forward-fill of missing dates — only trading days with actual candle data appear
- Uses current units (not historical) — represents what the portfolio would be worth if you had held these same units since each leg's open date

### Monthly Performance tab

Groups `portfolio_history` by `YYYY-MM`. For each month:
- **Open Value**: first trading day's reconstructed value in that month
- **Close Value**: last trading day's value
- **Monthly P&L $**: Close − Open
- **Monthly P&L %**: (Close − Open) / Open × 100
- **Cumulative P&L**: Close − first-ever Open value across the entire history

### Daily Performance tab (YTD)

Filters `portfolio_history` to the current calendar year (Jan 1 → today). Rows are sorted newest first. For each day:
- **Day P&L $**: today's value − yesterday's value
- **YTD P&L $**: today's value − first value of the year

---

## 7. SPY Benchmark & Alpha

SPY data is fetched via `yfinance.download("SPY", ...)` at the start of each export run.

### Monthly Alpha

```
alpha_pct[month] = portfolio_monthly_return% - SPY_monthly_return%
```

SPY monthly return is the percentage change in SPY's closing price across that calendar month (using `interval="1mo"` from yfinance, `auto_adjust=True`).

A positive alpha means the portfolio outperformed the S&P 500 that month.

### YTD Alpha (widget block)

```
portfolio_ytd% = (portfolio_value[today] / portfolio_value[Jan 1] - 1) × 100
spy_ytd%       = (SPY_close[today] / SPY_close[Jan 1] - 1) × 100
alpha_ytd%     = portfolio_ytd% - spy_ytd%
```

---

## 8. Sector Allocation

Sectors come from yfinance (`info["sector"]` per ticker). Position values are grouped by sector:

```python
sector_value[sector] += aggregate_position_value(ticker)
sector_pct[sector] = sector_value[sector] / total_portfolio_value × 100
```

Top 4 sectors by value are displayed, e.g.:
`Technology 68%  ·  Finance 12%  ·  Energy 8%  ·  Other 12%`

---

## 9. Average Hold Time

**Value-weighted** average number of days each position leg has been held:

```
avg_hold_days = Σ(days_held_i × leg_cost_basis_i) / Σ(leg_cost_basis_i)
```

Where `leg_cost_basis_i = units × open_price`. Days are calculated from `open_date` to today (UTC).

Value-weighting ensures a large $50k position held for 2 years contributes more to the average than a small $1k position held for 3 years.

---

## 10. Sheet Tab Reference

| Tab | Content | Updated each export |
|-----|---------|---------------------|
| **Dashboard** | Pinned first tab. Native Sheets charts (portfolio value, drawdown, monthly P&L vs SPY, signal distribution). Backed by `Chart Data`. | Yes — full overwrite |
| **Chart Data** | Numeric series feeding the Dashboard charts (resampled portfolio history, drawdown series, signal counts). Not formatted for reading. | Yes — full overwrite |
| **Looker - Daily** | Long-format daily portfolio + per-ticker rows for Looker Studio data source connection. | Yes — full overwrite |
| **Looker - Positions** | Long-format current positions table for Looker Studio. | Yes — full overwrite |
| **Positions** | All open legs — read-only mirror of `positions.json` (no reverse-sync). | Yes — full overwrite |
| **Overview** | Portfolio widget (rows 1–8) + all tickers: IC signal, **Macro Adj**, regime, indicators, P&L, fundamentals. **Close / Day-Change / P&L now use the live intraday eToro price on every run** (see v2.2). | Yes — full overwrite |
| **Live Overview** | Compact intraday dashboard: live price, day change, P&L, composite + reversal signals per ticker. Live price sourced from eToro hourly candles. | Yes — full overwrite |
| **Screener** | Machine-learning pre-breakout ranking over the whole liquid US market: Score, predicted growth **Window**, per-horizon probabilities (P10/P30/P90/P180), features, walk-forward backtest, **timing-reliability** table, sector base rates. | On market open / close / manual only |
| **Log Book** | Full OHLCV history (90 days) per ticker with indicators, sorted newest first. | Yes — full overwrite |
| **Daily P&L** | Append-only log: TOTAL + per-ticker rows per day; same-day re-runs replace that day's rows only. | Yes — append / replace today |
| **Monthly Performance** | Portfolio widget + monthly P&L from Sep 2023 → today with SPY Alpha. | Yes — full overwrite |
| **Daily Performance** | Portfolio widget + daily P&L YTD (Jan 1 → today), newest first. | Yes — full overwrite |
| **Closed Trades** | Realised trades imported from eToro account-statement CSV via `python main.py import-statement <file>`. | On import only |
| **Metadata** | Last updated timestamp, trigger, ticker list. | Yes — full overwrite |
| **NVDA / AMD / …** | Per-ticker tab. Visible area = company info, snapshot, signal weights, indicator analysis table, macro factors (sector, market cap, analyst target, 52-week range, short ratio, institutional %, recent news), positions, plus **7 embedded native charts** (RSI, MACD hist, BB%, EMA alignment, Fisher, ROC 5d/20d, Volume ratio). OHLCV + indicator time series (18 columns) live in **hidden columns Z–AL** and back the charts. | Yes — full overwrite |

---

## 11. Scheduler

`scheduler.py` runs an APScheduler `BlockingScheduler` daemon with **7 jobs**. Times in Berlin unless noted.

| # | Job | Cron | What it does | Misfire grace |
|---|-----|------|--------------|---------------|
| 1 | Market open | 15:30 Mon–Fri | Full Sheets export | 1800 s |
| 2 | Hourly | 16:30, 17:30, 18:30, 19:30, 20:30, 21:30 Mon–Fri | Live-only export (Overview widget + Daily Perf + per-ticker tabs) | 1800 s |
| 3 | Market close | 22:00 Mon–Fri | Full Sheets export | 1800 s |
| 4 | Daily OHLCV refresh | 23:00 daily | Position sync + OHLCV refresh + full Sheets export | 3600 s |
| 5 | NY midnight | 00:00 `America/New_York` daily (= 06:00 Berlin) | Full Sheets export — second daily push so the dashboard reflects the latest data at the start of each NY trading day. Provides natural redundancy if job 4 fails. | 1800 s |
| 6 | Pre-open position sync | 15:25 Mon–Fri | eToro → `positions.json` sync, 5 min before NYSE open (09:30 ET) so holdings are fresh going into the open export. | 1800 s |
| 7 | Pre-close position sync | 21:55 Mon–Fri | eToro → `positions.json` sync, 5 min before NYSE close (16:00 ET). | 1800 s |

The two position syncs are offset 5 minutes ahead of the open/close full exports so they never race those jobs for the write-lock (a same-minute collision could otherwise skip a full export).

**Concurrency lock.** Every DB-writing job holds an advisory file lock (`pipeline_lock.py`, `fcntl.flock` on `market_data.db.lock`). A manual CLI command that writes the database (`export` / `backfill` / `refresh` / `sync-positions` / `screener-backfill` / `update-macro` / `import-statement`) acquires the same lock and **aborts with a clear message if the scheduler is mid-write** (and vice-versa — a scheduled job skips to its next run if a manual command holds it). This structurally prevents the concurrent-writer race that can corrupt the SQLite file. The kernel releases the lock automatically if a process dies, so a crash never leaves a stale lock.

**Run as a daemon:** `python main.py scheduler`. The process is started on this Mac via the `com.trading.scheduler` LaunchAgent (`~/Library/LaunchAgents/com.trading.scheduler.plist`), which auto-respawns it on crash or kill.

**Sleep is the main failure mode** — APScheduler does not wake the host. A fire that lands while the Mac is in Standby/Maintenance Sleep is silently missed. The misfire grace times above only help if the host wakes within that window.

---

## 12. Pre-Breakout Screener (Multi-Horizon)

`screener.py` is a self-contained machine-learning system that scans the whole liquid
US market to rank names most likely to **break out** (rally ≥ +20%) and predicts the
**time window** in which the move is likely to land. It is independent of the P&L
dashboard: its own SQLite tables, its own yfinance backfill, its own model.

### 12.1 Data & universe

- **Price source:** yfinance daily OHLCV (split/dividend-adjusted), stored in the
  `screener_candles` table — separate from the eToro `candles` used by the dashboard.
- **Sector metadata:** `screener_meta` table (`sector`, `industry`) backfilled once per
  symbol from yfinance.
- **Discovery universe (`discovery_universe()`):** the liquidity-filtered active set ∪
  S&P 500 ∪ the wide watchlist ∪ current holdings (~2,200 names). Symbols without
  backfilled OHLCV drop out automatically.
- **Eligibility filter (`_eligible`):** price ≥ $5 and 20-day average dollar-volume ≥
  $20M, and all features present (needs ≥ 252 trading days of history, so freshly-listed
  names are excluded rather than imputed).

### 12.2 Features (`FEATURE_COLS`)

Ten purely-technical, point-in-time features per name: `pos_252` (position in the
trailing 252-day range), `dist_below_high`, `tightness_20` (volatility contraction),
`ma_stack` (MA20 > MA50 > MA200), `px_vs_ma50`, `rsi`, `rsi_slope_5`, `vol_contraction`,
`obv_slope` (accumulation), `trend_63`. `price` and `avg_dollar_vol` are carried for
filtering only — never fed to the model.

### 12.3 Multi-horizon labels

For each horizon **N ∈ {10, 30, 90, 180} trading days** (`SCREEN["HORIZONS"]`), a binary
label marks whether the max forward high reaches **+20%** (`RALLY_THRESHOLD`) within N
days (`make_multi_labels` → `label_h{N}`). The last N rows of each series are `NaN`
(window not yet closed) and simply drop out of that horizon's training set. Labels are
training targets only; they are never merged into the feature matrix.

### 12.4 Model — one gradient-boosted classifier per horizon

`MultiHorizonScorer` holds one `PreBreakoutScorer` (xgboost `XGBClassifier`, sklearn
`HistGradientBoosting` fallback) per horizon. Each sub-model predicts *P(+20% within its
horizon)*. The model is **retrained from scratch on all matured history every run** — no
online/streaming state.

- **Ranking score** = `P(PRIMARY_HORIZON)` (default 90d — kept near the 90-day trade
  hold so ranking and the backtest agree). Drives the top-N pick order.
- **Predicted growth window** = the **earliest** horizon whose probability clears
  `WINDOW_PROB_THRESHOLD` (0.5), e.g. `~30d`; `>180d` if none clear it.
- Probabilities are approximately monotone across horizons (`P10 ≤ P30 ≤ P90 ≤ P180`)
  because the windows are nested — treated as a sanity check, not enforced.

### 12.5 Walk-forward backtest (`walk_forward_backtest`)

The only honest measure of edge. Weekly rebalances snapped to real trading days; at each
date the scorer is trained only on rows whose forward label window closed **before** an
**embargo** sized to the longest horizon (≈180 trading days) — strict train-before-test,
no lookahead. Top-`TOP_N` (5) eligible names are entered and simulated forward with:

- **10% hard stop / 20% take-profit / 90-day time exit** (`simulate_trade`),
- **fees + slippage** on both sides (stop assumed to fill before target on an
  ambiguous bar — conservative).

Headline metrics: trades, win rate, avg return/trade, avg win vs avg loss, reward:risk,
max drawdown (1/N fixed-fractional), exit mix, and a per-year breakdown.

### 12.6 Timing reliability (the growth-window validation)

Separately from P&L, every backtest pick records its **predicted window** and the
**ground-truth days-to-target** (`time_to_target` — first forward day the +20% level is
touched, censored at 180d, ignoring stops). Two tables are produced:

- **By predicted window:** picks, % that actually hit within that window, % that ever
  hit, and median actual days-to-target. This directly answers *"when we say ~30 days,
  does it land in ~30 days?"*.
- **Per-horizon calibration:** mean predicted probability vs realised hit rate, plus a
  **Brier score** (lower = better) per horizon — showing which horizon models are
  trustworthy and which are over-confident.

Both render on the **Screener** tab and in the CLI backtest output.

### 12.7 CLI

```
python main.py screener-backfill market   # one-time whole-market yfinance backfill (~6.9k names)
python main.py screen  [wide|sp500|market] # today's ranked list + growth windows
python main.py backtest[wide|sp500|market] # walk-forward metrics + timing-reliability tables
```

Key config knobs live in `SCREEN` (`config.py`): `HORIZONS`, `PRIMARY_HORIZON`,
`WINDOW_PROB_THRESHOLD`, `RALLY_THRESHOLD`, `TOP_N`, `STOP_LOSS`, `TAKE_PROFIT`,
`MAX_HOLD_DAYS`, `RETRAIN_EVERY`, `MIN_PRICE`, `MIN_DOLLAR_VOL`, `LABEL_MODE`.

---

## Changelog

Each entry records: date, version, what changed, and why.
To add a new entry: prepend a new `### vX.Y` block immediately below this line.

---

### v2.4 — Probability Calibration Study + Data-Noise Reduction

**Date:** 2026-07-09

Goal: make the horizon probabilities (which drive the growth **Window**) trustworthy, and
cut noise in the data feeding the model. Every change was flag-gated and **validated
one-at-a-time in the walk-forward** (win rate / expectancy + per-horizon Brier and
mean-pred-vs-actual); only what improved out-of-sample was kept on.

**Headline finding:** the explicit probability calibrator (isotonic/sigmoid on a
time-ordered holdout) is *not* the best tool here — it improves the data-rich short
horizons but **overfits the data-starved long horizons** (180d Brier 0.31 → 0.37).
**Sample-uniqueness weighting + triple-barrier labels calibrate better as a byproduct**:
the overconfident 90d model went from **pred 61% / actual 50%** to **46% / 44%**, with the
best win rate/expectancy of any configuration. So the goal (trustworthy probabilities) was
met — via the method the backtest proved, not the textbook calibrator.

Kept ON (validated wins):
- **Sample-uniqueness weighting** (`SAMPLE_WEIGHTING`) — López de Prado average-uniqueness
  weights down-weight autocorrelated overlapping forward labels. Improved Brier at every
  horizon (90d 0.33 → 0.26) with flat win rate. `_avg_uniqueness_weights`, `w_h{N}` panel
  columns, passed to XGBoost `sample_weight`.
- **Triple-barrier labels** (`LABEL_MODE="triple_barrier"`) — train on "+TAKE_PROFIT hit
  **before** −STOP_LOSS within the horizon", matching the backtest's exit logic. Best win
  rate (46.5% → 49.0%) and expectancy; probabilities are now genuinely P(target-before-stop).
  `make_triple_barrier_labels`.
- **Input data hygiene** (`CLEAN_OHLCV`) — `_clean_ohlcv` in `load_ohlcv` drops non-positive
  O/H/L/C, `high<low`/inconsistent bars, zero-volume days, and single-bar spike-and-revert
  ticks (deviation vs a robust centered 5-day median; sustained real moves are kept).

Available but defaulted OFF (validation didn't justify them):
- **Explicit calibration** (`CALIBRATE`, `CALIBRATION_METHOD` isotonic|sigmoid,
  `CALIBRATION_HOLDOUT`, `CALIBRATION_MIN`) — implemented in `PreBreakoutScorer` (time-ordered
  holdout, no lookahead); overfits long horizons once weighting is on.
- **Cross-sectional feature normalization** (`CROSS_SECTIONAL_NORM` rank|zscore) — per-date
  rank/z-score of continuous features; neutral in validation.

---

### v2.3 — Concurrency Lock + Daily Position Sync

**Date:** 2026-07-08

#### Pipeline write-lock (`pipeline_lock.py`, new)

- Advisory `fcntl.flock` on `market_data.db.lock` serializing every database writer.
- **Root cause it fixes:** a manual `export`/`backfill` running *concurrently* with the
  always-on scheduler daemon (two SQLite writers), then killed mid-transaction, can
  truncate/corrupt the database.
- Manual write commands (`export`, `backfill`, `refresh`, `sync-positions`,
  `screener-backfill`, `update-macro`, `import-statement`) now acquire the lock and
  **abort with a clear message** if the scheduler is mid-write. Scheduler jobs acquire
  it too and **skip to their next run** if a manual command holds it.
- The kernel frees the lock automatically on process death — a crash never leaves a
  stale lock. `main.py` dispatch refactored into `_dispatch()` wrapped by the lock.

#### Scheduler — position-sync jobs around NYSE open & close

- Two new `sync_positions()` jobs timed to the exchange: **15:25 Berlin** (5 min before
  NYSE open, 09:30 ET) and **21:55 Berlin** (5 min before NYSE close, 16:00 ET), Mon–Fri,
  so `positions.json` is fresh going into the open/close exports. Offset 5 min ahead of
  those exports so they never race for the write-lock. Both lock-protected.

---

### v2.2 — Pre-Breakout Screener, Multi-Horizon Growth-Window Prediction, Overview Live-Price Fix

**Date:** 2026-07-08

Covers: the machine-learning screener (`screener.py`, new **Screener** tab), the
multi-horizon growth-window prediction, and a fix so the Overview tab reflects live
intraday prices on every scheduled run.

#### Pre-breakout screener (`screener.py`, new — Section 12)

- New standalone ML system scanning the whole liquid US market (~2,200-name discovery
  universe) for pre-breakout setups. Own yfinance backfill (`screener_candles`), own
  sector table (`screener_meta`), gradient-boosted scorer (xgboost / sklearn fallback).
- Ten point-in-time technical features; +20%-within-window labels; walk-forward backtest
  with embargoed train-before-test (no lookahead), 10% stop / 20% target / 90d exit,
  fees + slippage.
- New **Screener** sheet tab: ranked list, NEW IDEAS (top unheld names), feature
  importances, walk-forward metrics, per-year breakdown, sector base rates + charts.
- CLI: `screener-backfill`, `screen [wide|sp500|market]`, `backtest [wide|sp500|market]`.

#### Multi-horizon growth-window prediction

- The single "+20% in 60 days?" label is replaced by **four horizon classifiers at
  10 / 30 / 90 / 180 trading days** (`MultiHorizonScorer`), each retrained on its own
  matured history every run (retrain-on-history — no online loop).
- Each name now gets a predicted growth **Window** (earliest horizon whose probability
  clears `WINDOW_PROB_THRESHOLD` = 0.5) plus per-horizon probabilities **P10/P30/P90/P180**.
  Ranking uses `PRIMARY_HORIZON` = 90d (aligned with the 90-day trade hold).
- New **timing-reliability** validation: for every backtest pick, predicted window vs
  ground-truth days-to-target (`time_to_target`). Two tables — hit-rate/median-days by
  predicted window, and per-horizon calibration with **Brier scores** — surface on the
  Screener tab and in the CLI backtest. This is the honest test of the timing claim.
- Deliberately **not** doing cross-industry price imputation for young names (would inject
  fabricated data and corrupt the backtest); short-history names are filtered out instead.
- **Parallel training:** the four horizon classifiers are independent, so
  `MultiHorizonScorer.fit` trains them concurrently (`ThreadPoolExecutor`, xgboost
  releases the GIL) with the core budget split across horizons — ~2.9× faster than
  sequential, cancelling most of the 4-classifier cost. Falls back to sequential on error.
- New config (`SCREEN`): `HORIZONS`, `PRIMARY_HORIZON`, `WINDOW_PROB_THRESHOLD`.

#### Overview tab — live intraday prices (bug fix)

- **Bug:** the **Overview** tab re-queried `get_portfolio_summary()` internally and
  ignored the live-price-patched `portfolio` built in `run_export`, so its Close /
  Day-Change / P&L columns only moved once a day (the 23:00 DB refresh) and looked frozen
  on the hourly/intraday runs. The live eToro price only reached the separate **Live
  Overview** tab.
- **Fix:** `_export_overview` now receives the live-patched `portfolio` (like
  `_export_live_overview` already did) and derives Close, Day-Change % and P&L from the
  live intraday price on every run. Open/High/Low/Volume stay at the last completed daily
  bar.

---

### v2.1 — Per-Ticker Charts, Macro Cache, Dashboard, Looker, NY-Midnight Job

**Date:** 2026-06-15

Covers: Stage 6 (`93e1ad3`), Z-column fix (`e1b6dcc`), Stage 7 (`4f03bf1`), NY-midnight scheduler job (`0f30d72`).

#### Per-ticker tab redesign

- **7 native Google Sheets charts** embedded right of the visible content per ticker: RSI, MACD histogram, BB %, EMA alignment, Fisher Transform, ROC 5d/20d, Volume ratio.
- **Indicator analysis table** added per tab: each indicator → value + signal label + plain-English interpretation.
- **Macro factors block** added per tab: sector, industry, market cap, analyst target vs price, 52-week range, short ratio, institutional %, and 5 most recent news headlines.
- **OHLCV history extended from 7 to 18 columns** (now includes all indicator time series feeding the charts).
- **OHLCV + indicator data hidden in columns Z–AL.** Visible tab is narrative + analysis + charts only — the raw numeric grid no longer clutters the tab. Charts reference the hidden Z+ range.

#### Macro cache (`macro_cache.py`)

- New file. Daily yfinance fetch per ticker, JSON cache keyed by ticker + date, refreshed at most once per calendar day.
- Fields cached: news (5 latest), analyst mean/median price target, sector, industry, market-cap (formatted), 52-week high/low, short ratio, institutional ownership %.
- No additional API key required (uses yfinance).
- New CLI: `python main.py update-macro [TICK…]` — force refresh.
- `STX.US → STX` ticker alias for Yahoo.

#### Signal scoring — macro overlay (new Section 4.6)

- Composite signal now includes a `macro_adj` component using cached macro data.
- **Analyst-target gap**: ±0.20 / ±0.40 / ±0.75 based on how far the current close sits relative to the mean analyst target.
- **52-week proximity**: −0.25 within 3 % of the 52-week high (extension risk); +0.25 within 10 % of the 52-week low (mean-reversion candidate).
- Added to **both** the IC-weighted path and the legacy fixed-weight path.
- Surfaced as a new **Macro Adj** column in Overview, plus a `MacroAdj:±X.XX` suffix in the signal `reason` string.

#### Dashboard tab (pinned first)

- New tab. Native Sheets line/column/bar charts: portfolio value over time, drawdown series, monthly P&L vs SPY, signal distribution.
- Backed by a new **Chart Data** tab with the resampled numeric series (kept narrow so the Dashboard charts stay performant).
- Pinned to the first position on each export run via `_pin_dashboard_first`.

#### Looker Studio integration

- Two new tabs designed as Looker Studio data sources:
  - **Looker - Daily** — long-format daily portfolio + per-ticker rows.
  - **Looker - Positions** — long-format current open positions snapshot.

#### Quota / reliability fixes

- New `_write_z_data()` writer with 20 / 40 / 60 s retry ladder (matches `_write_tab`) for the hidden Z+ data range.
- 2 s throttle and retry inside `_add_ticker_charts` to avoid 429 bursts when re-creating the 7 charts × N tickers (previously hammered Sheets API on full exports).

#### Scheduler — new NY-midnight job

- 5th job added: full Sheets export at **00:00 `America/New_York` (= 06:00 Berlin) daily**.
- Provides a second daily push after the 23:00 Berlin refresh — dashboard reflects the latest data at the start of each NY trading day, and acts as natural redundancy if the 23:00 fire fails (see ops note: an instance of this exact failure happened on 2026-06-14 with a transient SQLite "unable to open database file" error during wake-from-sleep).
- Misfire grace: 1800 s.

#### Docs

- New [Section 11: Scheduler](#11-scheduler) — full job table, LaunchAgent note, and the macOS sleep-misfire caveat.

---

### v2.0 — Historical Performance, Adaptive Signal Scoring, Risk Metrics

**Date:** 2026-05-21

#### Data

- `INITIAL_CANDLES_COUNT` changed: **365 → 1000**
  - Reason: Portfolio positions go back to Sep 2023 (AMZN leg opened 2023-09-27); need >2 years of history for Monthly Performance. 1000 candles ≈ 4 years (back to ~2022), also provides sufficient history for IC weight computation (requires 90 days minimum, preferably 250).
  - One-time action: run `python main.py backfill` after this change to pull extended history.

#### New tabs

- **Monthly Performance**: portfolio-level, no ticker rows, one row per calendar month from Sep 2023 → running month. Columns: Month, Open Value, Close Value, Monthly P&L $, Monthly P&L %, SPY Return %, Alpha %, Cumulative P&L $, Cumulative P&L %.
- **Daily Performance**: YTD (Jan 1 → today), one row per trading day, newest first. Columns: Date, Portfolio Value, Day P&L $, Day P&L %, YTD P&L $, YTD P&L %.
- Both tabs include an 8-row **portfolio summary widget block** (rows 1–8) before data headers.

#### Overview tab

- 8-row portfolio summary widget block prepended above existing ticker table (headers shift to row 10, data to row 11+).
- New ticker columns: **Regime** (TRENDING / HIGH_VOL / RANGING) and **Top Signal** (highest absolute-IC signal for that ticker).
- New indicator columns: **ROC 5d** and **ROC 20d**.

#### Per-ticker tabs

- New **SIGNAL WEIGHTS** section between Snapshot and Market Sentiment: shows regime, IC weight per signal (7 signals), directional interpretation (Bullish/Bearish/Neutral per sign of IC weight), and top signal name.

#### Signal scoring — IC-weighted adaptive path

- **Replaced static hardcoded weights** with IC (Information Coefficient) based weights computed fresh each export run.
- IC = Pearson correlation of each signal vs 5-day forward return over rolling 90-day window.
- Weights normalised so Σ|w| = 1. Negative IC retained (contrarian signal).
- Regime multipliers applied on top of IC weights, then re-normalised.
- Original fixed-weight path retained as fallback (Log Book tab, or when < 40 candles available).
- New helper `_signal_direction()` converts indicator dict values to ±1 directional scores before IC weighting.

#### Regime detection

- New `_detect_regime(spy_daily_df, portfolio_returns)` classifies market as TRENDING / HIGH_VOL / RANGING each run.
- SPY daily data fetched via yfinance (`period="5y"`, `interval="1d"`, `auto_adjust=True`) at start of each export.
- Portfolio 20-day annualised realised vol computed from reconstructed portfolio return series.
- Regime shared across all tickers in that export run (portfolio-level, not per-ticker).

#### Risk metrics widget block

- New widget block on Overview, Monthly Performance, and Daily Performance tabs (all share the same `_build_widget_rows()` call — computed once per export).
- Five new portfolio risk metrics:
  - **Annualised Volatility (σ)** with 1–10 score
  - **VaR 95%** — historical simulation, 5th percentile of daily returns
  - **Sortino Ratio** — annual return / annualised downside deviation (risk-free = 0%)
  - **Beta + HHI Composite** — value-weighted portfolio beta + HHI concentration, scored 1–10
  - **Max Drawdown** — peak-to-trough from reconstructed portfolio history

#### Concentration metric change

- Previous: flat threshold bonus (`+1.5 if largest position > 25%` style)
- New: **HHI (Herfindahl-Hirschman Index)** × 3.0 as continuous concentration component
- Reason: threshold approach had cliff effects (19.9% and 20.1% concentration indistinguishable). HHI is proportional, continuous, and the academic/regulatory standard.

#### SPY benchmark

- SPY monthly returns fetched via yfinance `interval="1mo"` — used for Alpha % column in Monthly Performance tab.
- YTD SPY and portfolio returns compared in all three widget blocks.

#### Additional portfolio metrics in widget

- `_compute_sector_allocation()`: top-4 sectors by current position value shown as % breakdown.
- `_compute_avg_hold_time()`: value-weighted average days held across all open legs.

---

### v1.1 — Positions & Daily P&L Log

**Date:** Second sprint (2025)

- `positions.json` introduced — stores open position legs (ticker, direction, units, entry price, open date, stop loss, take profit)
- P&L calculation per leg and aggregated per ticker (`_position_pnl`, `_aggregate_pnl`)
- **Positions tab** in sheet — editable; synced back to `positions.json` at the start of each export run
- **Daily P&L tab** — append-only log; TOTAL row + per-ticker P&L rows written each export; re-running same day replaces that day's rows only, historical rows preserved; newest rows at top
- eToro portfolio API sync (`sync_positions()` / `python main.py sync-positions`) — auto-populates `positions.json` from live open positions
- Dynamic ticker list — tickers auto-discovered from `positions.json` (not just watchlist); handles IONQ, WDC, SNDK etc. that appear in positions but not in watchlist; new tickers automatically backfilled before export

---

### v1.0 — Initial Build

**Date:** First sprint (2025)

- eToro API client (`etoro_client.py`) — candle fetch, instrument search, portfolio pull
- SQLite schema (`database.py`) — instruments, candles, fetch_log tables; upsert-safe
- Backfill and daily refresh pipeline (`pipeline.py`) — `backfill()` (full history), `refresh()` (stale only), `refresh_single()`
- Google Sheets export (`sheets_exporter.py`) — Overview, Log Book, per-ticker tabs
- APScheduler daemon triggering export at market open (09:30 NY) and close (16:00 NY)
- Fixed-weight composite signal scoring: EMA alignment (±2) + RSI (±1) + MACD line (±1) + MACD histogram (±0.25) + Fisher Transform (±0.5) + Bollinger Bands (±0.5) + Volume multiplier [0.75–1.5]
- Fundamentals via yfinance: P/E, forward P/E, EPS, revenue growth, profit margin, D/E, beta, analyst target, earnings date, recent news
- Market sentiment narrative (`_market_sentiment`) — 4-sentence plain English summary per ticker
- Candle pattern classification (`_candle_type`) — Doji, Hammer, Shooting Star, Bullish, Bearish etc.
