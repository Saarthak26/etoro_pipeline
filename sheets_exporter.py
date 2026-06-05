"""
sheets_exporter.py — Exports all trading data to Google Sheets.

Tab structure:
  • Overview        — All tickers: price, day change %, trend, every indicator
  • Log Book        — Full chronological daily record for all tickers with fundamentals
  • NVDA / AMD / …  — One tab per ticker: indicators + full OHLCV history
  • Metadata        — Last updated timestamp and run trigger

Auth: Google service account JSON key pointed to by GOOGLE_SHEETS_CREDENTIALS_PATH.
"""

from __future__ import annotations

import logging
import os
import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_BERLIN = ZoneInfo("Europe/Berlin")

import numpy as np
import pandas as pd
import ta
import yfinance as yf

import gspread
from google.oauth2.service_account import Credentials

from database import get_candles, get_portfolio_summary
from config import (
    WATCHLIST_TICKERS,
    COMPANY_INFO,
    GOOGLE_SHEETS_CREDENTIALS_PATH,
    GOOGLE_SHEET_ID,
    GOOGLE_SHEET_NAME,
    POSITIONS_PATH,
    INITIAL_CASH,
)

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

TAB_POSITIONS    = "Positions"
TAB_OVERVIEW     = "Overview"
TAB_LOGBOOK      = "Log Book"
TAB_DAILY_PNL    = "Daily P&L"
TAB_MONTHLY_PERF = "Monthly Performance"
TAB_DAILY_PERF   = "Daily Performance"
TAB_LIVE         = "Live Overview"
TAB_CLOSED       = "Closed Trades"
TAB_META         = "Metadata"
STATIC_TABS      = [TAB_POSITIONS, TAB_OVERVIEW, TAB_LOGBOOK, TAB_DAILY_PNL,
                    TAB_MONTHLY_PERF, TAB_DAILY_PERF, TAB_LIVE, TAB_CLOSED, TAB_META]

# Regime multipliers: scale IC weights up/down based on market environment
REGIME_MULTIPLIERS = {
    "TRENDING": {"rsi": 0.6, "bb_position": 0.6, "macd_hist": 1.5,
                 "ema_score": 1.5, "fisher": 1.2, "roc_5": 1.3, "roc_20": 1.3},
    "HIGH_VOL": {"rsi": 1.5, "bb_position": 1.5, "macd_hist": 0.6,
                 "ema_score": 0.6, "fisher": 0.8, "roc_5": 0.7, "roc_20": 0.7},
    "RANGING":  {"rsi": 1.0, "bb_position": 1.0, "macd_hist": 1.0,
                 "ema_score": 1.0, "fisher": 1.0, "roc_5": 1.0, "roc_20": 1.0},
}

POSITIONS_HEADERS = ["Ticker", "Direction", "Units", "Entry Price", "Open Date", "Stop Loss", "Take Profit"]


# ── Positions loader ──────────────────────────────────────────────────────────

def _load_positions() -> dict[str, list[dict]]:
    """Return ticker → list of open positions. Skips zero-unit entries."""
    if not os.path.exists(POSITIONS_PATH):
        return {}
    try:
        with open(POSITIONS_PATH) as f:
            raw = json.load(f)
        result: dict[str, list[dict]] = {}
        for p in raw:
            if not (p.get("units", 0) and float(p.get("units", 0)) > 0):
                continue
            result.setdefault(p["ticker"], []).append(p)
        return result
    except Exception as e:
        log.warning("Could not load positions.json: %s", e)
        return {}


def _position_pnl(pos: dict, current_price: float) -> dict:
    """Calculate P&L for a single position leg."""
    direction  = pos.get("direction", "BUY").upper()
    units      = float(pos.get("units", 0))
    open_price = float(pos.get("open_price", 0))
    if open_price <= 0 or units <= 0:
        return {}
    cost    = units * open_price
    value   = units * current_price
    raw_pnl = (value - cost) if direction == "BUY" else (cost - value)
    return {
        "direction":      direction,
        "units":          units,
        "open_price":     open_price,
        "open_date":      pos.get("open_date", ""),
        "stop_loss":      pos.get("stop_loss") or "",
        "take_profit":    pos.get("take_profit") or "",
        "cost_basis":     round(cost, 2),
        "current_value":  round(value, 2),
        "unrealized_pnl": round(raw_pnl, 2),
        "pnl_pct":        round(raw_pnl / cost * 100, 2) if cost else 0,
    }


def _write_positions_tab(spreadsheet: gspread.Spreadsheet, positions: dict[str, list[dict]], tickers: list[str]):
    """Write all positions to the Positions tab so the user can edit them in the sheet."""
    rows = [POSITIONS_HEADERS]
    for ticker in tickers:
        legs = positions.get(ticker, [])
        if legs:
            for leg in legs:
                rows.append([
                    ticker,
                    leg.get("direction", "BUY"),
                    leg.get("units", 0),
                    leg.get("open_price", 0),
                    leg.get("open_date", ""),
                    leg.get("stop_loss") or "",
                    leg.get("take_profit") or "",
                ])
        else:
            # Placeholder row so every ticker is visible for easy editing
            rows.append([ticker, "BUY", 0, 0, "", "", ""])
    _write_tab(spreadsheet, TAB_POSITIONS, rows)


def _sync_positions_from_sheet(spreadsheet: gspread.Spreadsheet):
    """
    Read the Positions tab from Google Sheets and overwrite positions.json.
    Called at the start of every export so the sheet is always the source of truth.
    """
    try:
        ws   = spreadsheet.worksheet(TAB_POSITIONS)
        rows = ws.get_all_values()
    except Exception as e:
        log.warning("Could not read Positions tab: %s", e)
        return

    if not rows or rows[0] != POSITIONS_HEADERS:
        log.warning("Positions tab has unexpected headers — skipping sync.")
        return

    positions = []
    for row in rows[1:]:
        if len(row) < 3:
            continue
        try:
            ticker    = str(row[0]).strip().upper()
            direction = str(row[1]).strip().upper() or "BUY"
            units     = float(row[2]) if row[2] else 0
            price     = float(row[3]) if len(row) > 3 and row[3] else 0
            date      = str(row[4]).strip() if len(row) > 4 else ""
            sl        = float(row[5]) if len(row) > 5 and row[5] else None
            tp        = float(row[6]) if len(row) > 6 and row[6] else None
            if not ticker:
                continue
            positions.append({
                "ticker":     ticker,
                "direction":  direction,
                "units":      units,
                "open_price": price,
                "open_date":  date,
                "stop_loss":  sl,
                "take_profit":tp,
            })
        except (ValueError, IndexError) as e:
            log.debug("Skipping malformed positions row %s: %s", row, e)

    with open(POSITIONS_PATH, "w") as f:
        json.dump(positions, f, indent=2)

    active = sum(1 for p in positions if p["units"] > 0)
    log.info("Synced Positions tab → positions.json (%d active legs across %d rows)",
             active, len(positions))


def _aggregate_pnl(positions: list[dict], current_price: float) -> dict:
    """Aggregate P&L across multiple position legs for the same ticker."""
    legs = [_position_pnl(p, current_price) for p in positions]
    legs = [l for l in legs if l]
    if not legs:
        return {}
    total_units = sum(l["units"] for l in legs)
    total_cost  = sum(l["cost_basis"] for l in legs)
    total_value = sum(l["current_value"] for l in legs)
    total_pnl   = sum(l["unrealized_pnl"] for l in legs)
    avg_entry   = round(total_cost / total_units, 4) if total_units else 0
    pnl_pct     = round(total_pnl / total_cost * 100, 2) if total_cost else 0
    return {
        "legs":           legs,
        "total_units":    round(total_units, 4),
        "avg_entry":      avg_entry,
        "total_cost":     round(total_cost, 2),
        "total_value":    round(total_value, 2),
        "total_pnl":      round(total_pnl, 2),
        "pnl_pct":        pnl_pct,
        "direction":      legs[0]["direction"],
    }


# ── Effective ticker list ─────────────────────────────────────────────────────

def _get_effective_tickers(positions: dict) -> list[str]:
    """Return config watchlist + any tickers from open positions, deduped, order preserved."""
    seen: set[str] = set()
    result: list[str] = []
    for t in WATCHLIST_TICKERS:
        if t not in seen:
            seen.add(t)
            result.append(t)
    for t in positions:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


# ── Position date parser ─────────────────────────────────────────────────────

def _parse_position_date(date_str: str) -> str:
    """Convert 'D-M-YYYY' open_date format to 'YYYY-MM-DD'. Returns '' if unparseable."""
    if not date_str:
        return ""
    parts = date_str.split("-")
    if len(parts) == 3:
        try:
            d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
            return f"{y:04d}-{m:02d}-{d:02d}"
        except ValueError:
            pass
    return date_str[:10] if len(date_str) >= 10 else ""


# ── Portfolio history reconstruction ─────────────────────────────────────────

def _build_portfolio_history(positions: dict, from_date: str, to_date: str) -> list[dict]:
    """
    Reconstruct daily portfolio value (sum of close × units) across all tickers
    for each trading day in [from_date, to_date]. Only includes position legs that
    were actually open on that date (respects open_date from positions.json).

    Forward-fills each ticker's last known close on dates where no candle exists
    (e.g. when a market is closed for one exchange but open for others, or when
    today's candle hasn't been fetched yet). This prevents phantom losses from
    tickers simply having stale data.

    Returns list of {date, value} sorted oldest → newest.
    """
    from database import get_candles_range

    # Step 1: build per-ticker date→close map (one fetch per ticker, not per leg)
    ticker_close_map: dict[str, dict[str, float]] = {}
    for ticker, legs in positions.items():
        if not any(float(l.get("units", 0)) > 0 for l in legs):
            continue
        earliest_leg = min(
            (_parse_position_date(l.get("open_date", "")) or from_date for l in legs
             if float(l.get("units", 0)) > 0),
            default=from_date,
        )
        candles = get_candles_range(ticker, max(from_date, earliest_leg), to_date)
        if candles:
            ticker_close_map[ticker] = {c["date"]: float(c["close"]) for c in candles}

    # Step 2: collect all unique trading dates across all tickers
    all_dates = sorted({d for closes in ticker_close_map.values() for d in closes})
    if not all_dates:
        return []

    # Step 3: seed every date with INITIAL_CASH (total funds deposited to eToro, in USD).
    # Then add unrealised P&L per leg: units × (close − entry) for BUY, reversed for SELL.
    # Formula: portfolio_value = INITIAL_CASH + unrealised_PnL + realised_PnL
    # This matches eToro's equity view: total_deposited + all_gains_to_date.
    date_value: dict[str, float] = {d: float(INITIAL_CASH) for d in all_dates}

    for ticker, legs in positions.items():
        date_close = ticker_close_map.get(ticker, {})
        if not date_close:
            continue
        for leg in legs:
            units = float(leg.get("units", 0))
            if units <= 0:
                continue
            entry     = float(leg.get("open_price", 0))
            direction = leg.get("direction", "BUY").upper()
            leg_start = _parse_position_date(leg.get("open_date", "")) or from_date
            last_known: float | None = None
            for date in all_dates:
                if date < leg_start:
                    continue
                if date in date_close:
                    last_known = date_close[date]
                if last_known is not None and entry > 0:
                    pnl = (last_known - entry) if direction == "BUY" else (entry - last_known)
                    date_value[date] += units * pnl

    # Add cumulative realised P&L from closed positions.
    try:
        from database import get_closed_positions
        closed = get_closed_positions(from_date=from_date, to_date=to_date)
        if closed:
            closed_sorted = sorted(closed, key=lambda x: x.get("close_date") or "")
            for date in all_dates:
                realized_as_of = sum(
                    float(cp.get("realized_pnl") or 0)
                    for cp in closed_sorted
                    if (cp.get("close_date") or "") <= date
                )
                date_value[date] += realized_as_of
    except Exception as e:
        log.debug("Could not load closed positions for history: %s", e)

    return [{"date": d, "value": round(v, 2)} for d, v in sorted(date_value.items())]


# ── Risk metrics ──────────────────────────────────────────────────────────────

def _compute_risk_metrics(
    positions: dict,
    portfolio: dict,
    fundamentals: dict,
    portfolio_history: list[dict],
    portfolio_returns: list[float],
) -> dict:
    """
    Compute σ (annualised vol), VaR 95%, Sortino ratio, Portfolio Beta + HHI composite,
    and Max Drawdown from actual portfolio return history.
    """
    empty = {
        "volatility_annual": None, "volatility_score": None, "volatility_label": "N/A",
        "var_95_pct": None, "var_95_dollar": None,
        "sortino_ratio": None,
        "portfolio_beta": None, "hhi": None, "composite_score": None, "composite_label": "N/A",
        "max_drawdown_pct": None, "max_drawdown_date": "",
    }
    if len(portfolio_returns) < 20:
        return empty

    rets = np.array(portfolio_returns, dtype=float)

    # 1. Annualised volatility
    sigma_annual = float(np.std(rets)) * (252 ** 0.5)
    vol_score    = round(min(10.0, max(1.0, sigma_annual / 0.06)), 1)
    vol_label    = ("Very High" if vol_score >= 8 else "High" if vol_score >= 6
                    else "Medium" if vol_score >= 4 else "Low")

    # 2. Historical VaR 95%
    var_pct    = float(np.percentile(rets, 5))
    total_val  = sum(
        _aggregate_pnl(legs, float(portfolio.get(t, {}).get("latest_close") or 0)).get("total_value", 0)
        for t, legs in positions.items() if legs
    )
    var_dollar = round(var_pct * total_val, 2)

    # 3. Sortino ratio (risk-free = 0%)
    downside = rets[rets < 0]
    sortino  = None
    if len(downside) >= 5:
        down_std = float(np.std(downside)) * (252 ** 0.5)
        ann_ret  = float((1 + np.mean(rets)) ** 252 - 1)
        sortino  = round(ann_ret / down_std, 2) if down_std > 0 else None

    # 4. Portfolio Beta + HHI composite
    total_value    = 0.0
    beta_weighted  = 0.0
    ticker_values: dict[str, float] = {}
    for ticker, legs in positions.items():
        if not legs:
            continue
        price = float(portfolio.get(ticker, {}).get("latest_close") or 0)
        if not price:
            continue
        agg = _aggregate_pnl(legs, price)
        if not agg:
            continue
        val   = agg["total_value"]
        beta  = _safe_num(fundamentals.get(ticker, {}).get("beta")) or 1.0
        ticker_values[ticker] = val
        total_value   += val
        beta_weighted += val * beta

    portfolio_beta = composite_score = hhi = None
    composite_label = "N/A"
    if total_value > 0:
        portfolio_beta  = round(beta_weighted / total_value, 2)
        hhi             = round(sum((v / total_value) ** 2 for v in ticker_values.values()), 3)
        raw_composite   = portfolio_beta * 4.5 + hhi * 3.0
        composite_score = round(min(10.0, max(1.0, raw_composite)), 1)
        composite_label = ("Very High" if composite_score >= 8 else "High" if composite_score >= 6
                           else "Medium" if composite_score >= 4 else "Low")

    # 5. Max drawdown
    max_dd = 0.0
    max_dd_date = ""
    if portfolio_history:
        peak = portfolio_history[0]["value"]
        for entry in portfolio_history:
            v    = entry["value"]
            peak = max(peak, v)
            dd   = (peak - v) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd      = dd
                max_dd_date = entry["date"]

    return {
        "volatility_annual":  round(sigma_annual, 4),
        "volatility_score":   vol_score,
        "volatility_label":   vol_label,
        "var_95_pct":         round(var_pct, 4),
        "var_95_dollar":      var_dollar,
        "sortino_ratio":      sortino,
        "portfolio_beta":     portfolio_beta,
        "hhi":                hhi,
        "composite_score":    composite_score,
        "composite_label":    composite_label,
        "max_drawdown_pct":   round(max_dd * 100, 2),
        "max_drawdown_date":  max_dd_date,
    }


# ── Adaptive signal weights (IC-based) ───────────────────────────────────────

def _compute_signal_weights(df: pd.DataFrame) -> dict:
    """
    Rolling 90-day Information Coefficient (IC) per signal vs 5-day forward return.
    IC = Pearson correlation(signal_series, forward_return).
    Returns weights normalised so Σ|w| = 1. Negative IC = contrarian weight (kept as-is).
    """
    if df is None or len(df) < 40:
        return {}

    close = df["close"]

    # Compute signal series
    rsi_s      = ta.momentum.RSIIndicator(close, window=14).rsi()
    macd_hist_s = ta.trend.MACD(close).macd_diff()
    ema50_s    = ta.trend.EMAIndicator(close, window=50).ema_indicator()
    ema_score_s = ((close - ema50_s) / ema50_s.replace(0, np.nan))

    period = 9
    hh  = df["high"].rolling(period).max()
    ll  = df["low"].rolling(period).min()
    hl  = (hh - ll).replace(0, 0.001)
    val = (2 * ((close - ll) / hl) - 1).clip(-0.999, 0.999)
    fisher_s = 0.5 * np.log((1 + val) / (1 - val))

    bb      = ta.volatility.BollingerBands(close, window=20)
    bb_rng  = (bb.bollinger_hband() - bb.bollinger_lband()).replace(0, np.nan)
    bb_pos_s = (close - bb.bollinger_lband()) / bb_rng - 0.5

    roc5_s  = close.pct_change(5)
    roc20_s = close.pct_change(20)

    signals = {
        "rsi":        rsi_s,
        "macd_hist":  macd_hist_s,
        "ema_score":  ema_score_s,
        "fisher":     fisher_s,
        "bb_position": bb_pos_s,
        "roc_5":      roc5_s,
        "roc_20":     roc20_s,
    }

    forward_ret = close.pct_change(5).shift(-5)
    window      = min(90, len(df) - 10)

    ics: dict[str, float] = {}
    for name, series in signals.items():
        s_win   = series.iloc[-window:]
        f_win   = forward_ret.iloc[-window:]
        valid   = s_win.notna() & f_win.notna()
        if valid.sum() < 15:
            ics[name] = 0.0
        else:
            ic = float(s_win[valid].corr(f_win[valid]))
            ics[name] = ic if not np.isnan(ic) else 0.0

    total = sum(abs(v) for v in ics.values()) or 1.0
    return {k: v / total for k, v in ics.items()}


def _apply_regime_multipliers(weights: dict, regime: str) -> dict:
    """Apply regime-specific multipliers on top of IC weights, then re-normalise."""
    mults    = REGIME_MULTIPLIERS.get(regime, REGIME_MULTIPLIERS["RANGING"])
    adjusted = {k: weights.get(k, 0.0) * mults.get(k, 1.0) for k in mults}
    total    = sum(abs(v) for v in adjusted.values()) or 1.0
    return {k: v / total for k, v in adjusted.items()}


def _detect_regime(spy_df, portfolio_returns: list[float]) -> dict:
    """
    Classify market into TRENDING / HIGH_VOL / RANGING using:
    - SPY position vs 200-day EMA
    - Portfolio realised volatility (20-day annualised)
    """
    regime          = "RANGING"
    spy_above_200ma = False
    realised_vol    = 0.0
    try:
        if spy_df is not None and len(spy_df) >= 50:
            spy_close       = spy_df["close"].astype(float)
            ema200          = spy_close.ewm(span=200, adjust=False).mean()
            spy_above_200ma = float(spy_close.iloc[-1]) > float(ema200.iloc[-1])
        if len(portfolio_returns) >= 10:
            realised_vol = float(pd.Series(portfolio_returns[-20:]).std() * (252 ** 0.5))
        if spy_above_200ma and realised_vol < 0.25:
            regime = "TRENDING"
        elif realised_vol > 0.40:
            regime = "HIGH_VOL"
    except Exception as e:
        log.debug("Regime detection failed: %s", e)
    return {"regime": regime, "spy_above_200ma": spy_above_200ma, "realised_vol": round(realised_vol, 4)}


def _signal_direction(signal: str, ind: dict, close: float) -> float:
    """Convert a signal name + indicator dict into a directional ±1 score for IC-weighted scoring."""
    if signal == "rsi":
        rsi = ind.get("rsi")
        if not isinstance(rsi, float):
            return 0.0
        if rsi >= 70:   return -1.0
        if rsi >= 60:   return  0.5
        if rsi <= 30:   return  1.0
        if rsi <= 40:   return -0.5
        return 0.0
    if signal == "macd_hist":
        mh = ind.get("macd_hist")
        if not isinstance(mh, float):
            return 0.0
        return 1.0 if mh > 0 else (-1.0 if mh < 0 else 0.0)
    if signal == "ema_score":
        e50, e200 = ind.get("ema50"), ind.get("ema200")
        if isinstance(e50, float) and isinstance(e200, float) and e50 > 0:
            score = (close - e50) / e50
            return max(-1.0, min(1.0, score * 10))
        return 0.0
    if signal == "fisher":
        f, fs = ind.get("fisher"), ind.get("fisher_signal")
        if isinstance(f, float) and isinstance(fs, float):
            return 1.0 if f > fs else -1.0
        return 0.0
    if signal == "bb_position":
        bbu, bbl = ind.get("bb_upper"), ind.get("bb_lower")
        if isinstance(bbu, float) and isinstance(bbl, float) and bbu != bbl:
            pos = (close - bbl) / (bbu - bbl) - 0.5   # range ≈ [-0.5, 0.5]
            return max(-1.0, min(1.0, pos * 2))
        return 0.0
    if signal in ("roc_5", "roc_20"):
        roc = ind.get(signal)
        if isinstance(roc, float):
            return 1.0 if roc > 0 else (-1.0 if roc < 0 else 0.0)
        return 0.0
    return 0.0


# ── Portfolio-level summary helpers ──────────────────────────────────────────

def _compute_avg_hold_time(positions: dict, portfolio: dict) -> int:
    """Value-weighted average days held across all open position legs."""
    from datetime import date as date_cls
    today      = datetime.now(timezone.utc).date()
    val_days   = 0.0
    total_val  = 0.0
    for ticker, legs in positions.items():
        price = float(portfolio.get(ticker, {}).get("latest_close") or 0)
        for leg in legs:
            units = float(leg.get("units", 0))
            if units <= 0:
                continue
            leg_val       = units * (float(leg.get("open_price") or 0) or price)
            open_iso      = _parse_position_date(leg.get("open_date", ""))
            if not open_iso:
                continue
            try:
                open_date = datetime.strptime(open_iso, "%Y-%m-%d").date()
                days      = (today - open_date).days
                val_days  += days * leg_val
                total_val += leg_val
            except Exception:
                pass
    return int(val_days / total_val) if total_val > 0 else 0


def _compute_sector_allocation(positions: dict, portfolio: dict, fundamentals: dict) -> str:
    """Return top-4 sectors as '% · %' string, e.g. 'Technology 68% · Finance 12%'."""
    sector_vals: dict[str, float] = {}
    total = 0.0
    for ticker, legs in positions.items():
        price = float(portfolio.get(ticker, {}).get("latest_close") or 0)
        if not price:
            continue
        agg = _aggregate_pnl(legs, price)
        if not agg:
            continue
        sector = (fundamentals.get(ticker, {}).get("sector") or "Other").strip() or "Other"
        sector_vals[sector] = sector_vals.get(sector, 0.0) + agg["total_value"]
        total += agg["total_value"]
    if not total:
        return "N/A"
    top4 = sorted(sector_vals.items(), key=lambda x: x[1], reverse=True)[:4]
    return "  ·  ".join(f"{s} {v / total * 100:.0f}%" for s, v in top4)


def _build_widget_rows(
    risk: dict,
    positions: dict,
    portfolio: dict,
    fundamentals: dict,
    spy_ytd_return,
    portfolio_ytd_return,
    timestamp: str,
) -> list[list]:
    """Return 8 rows for the portfolio summary widget block (shared across all performance tabs)."""
    # Aggregate totals
    total_invested = total_value = total_pnl = 0.0
    n_positions = 0
    for ticker, legs in positions.items():
        price = float(portfolio.get(ticker, {}).get("latest_close") or 0)
        if not price:
            continue
        agg = _aggregate_pnl(legs, price)
        if not agg:
            continue
        total_invested += agg["total_cost"]
        total_value    += agg["total_value"]
        total_pnl      += agg["total_pnl"]
        n_positions    += len(agg["legs"])

    pnl_pct   = round(total_pnl / total_invested * 100, 2) if total_invested else 0
    avg_hold  = _compute_avg_hold_time(positions, portfolio)
    sector_str = _compute_sector_allocation(positions, portfolio, fundamentals)

    # YTD Alpha string
    if spy_ytd_return is not None and portfolio_ytd_return is not None:
        alpha     = portfolio_ytd_return - spy_ytd_return
        alpha_str = (f"Portfolio {portfolio_ytd_return:+.1f}%  ·  "
                     f"SPY {spy_ytd_return:+.1f}%  ·  Alpha {alpha:+.1f}%")
    else:
        alpha_str = "N/A"

    # Risk value strings
    vol_str = (f"{risk['volatility_annual']*100:.1f}%  ·  "
               f"{risk['volatility_label']} ({risk['volatility_score']})"
               if risk.get("volatility_annual") is not None else "N/A")
    var_str = (f"{risk['var_95_pct']*100:.2f}%  ·  ${abs(risk['var_95_dollar']):,.0f}/day"
               if risk.get("var_95_pct") is not None else "N/A")
    sortino_str = (f"{risk['sortino_ratio']:.2f}"
                   if risk.get("sortino_ratio") is not None else "N/A")
    beta_str = (f"{risk['composite_score']}  ·  {risk['composite_label']}"
                f"  (β={risk['portfolio_beta']}, HHI={risk['hhi']})"
                if risk.get("composite_score") is not None else "N/A")
    dd_str   = (f"-{risk['max_drawdown_pct']:.1f}%  ({risk['max_drawdown_date']})"
                if risk.get("max_drawdown_pct") else "N/A")

    return [
        [f"PORTFOLIO SUMMARY — as of {timestamp}"],
        ["Total Invested", "Total Unrealized P&L", "P&L %", "# Positions", "Avg Hold Time"],
        [f"${total_invested:,.0f}", f"${total_pnl:+,.0f}", f"{pnl_pct:+.2f}%",
         str(n_positions), f"{avg_hold} days"],
        ["Volatility (σ)", "VaR 95%", "Sortino Ratio", "Beta + HHI Score", "Max Drawdown"],
        [vol_str, var_str, sortino_str, beta_str, dd_str],
        ["Sector Allocation", "", "", "", "YTD vs SPY (Alpha)"],
        [sector_str, "", "", "", alpha_str],
        [""],
    ]


# ── Live price fetch (eToro rates API) ───────────────────────────────────────

def _fetch_live_prices(tickers: list[str]) -> dict[str, float]:
    """
    Fetch the most recent 1-hour candle close from eToro for every ticker.
    Only overrides a ticker's price when the candle is from today (more current
    than the daily close). Returns {ticker: price}; missing = use DB close.
    """
    from etoro_client import EToroClient, EToroAPIError
    from database import get_instrument_id

    client = EToroClient()
    live: dict[str, float] = {}
    today = datetime.now(_BERLIN).strftime("%Y-%m-%d")

    for ticker in tickers:
        iid = get_instrument_id(ticker)
        if not iid:
            continue
        try:
            # Fetch last 3 hourly candles (desc = newest first)
            candles = client.get_candles(iid, "OneHour", 3, "desc")
            if not candles:
                continue
            latest = candles[0]
            candle_date = (latest.get("fromDate") or "")[:10]
            if candle_date >= today:
                price = latest.get("close")
                if price and float(price) > 0:
                    live[ticker] = round(float(price), 4)
        except EToroAPIError as e:
            log.debug("Intraday candle fetch failed for %s: %s", ticker, e)
        except Exception as e:
            log.debug("Unexpected error fetching intraday price for %s: %s", ticker, e)

    log.info("Live intraday prices fetched for %d/%d tickers from eToro", len(live), len(tickers))
    return live


# ── Google Sheets client ──────────────────────────────────────────────────────

def _get_client() -> gspread.Client:
    if not os.path.exists(GOOGLE_SHEETS_CREDENTIALS_PATH):
        raise FileNotFoundError(
            f"Service account credentials not found at: {GOOGLE_SHEETS_CREDENTIALS_PATH}\n"
            "Run: python main.py setup-sheets  for setup instructions."
        )
    creds = Credentials.from_service_account_file(GOOGLE_SHEETS_CREDENTIALS_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def _open_spreadsheet(client: gspread.Client) -> gspread.Spreadsheet:
    if GOOGLE_SHEET_ID:
        return client.open_by_key(GOOGLE_SHEET_ID)
    return client.open(GOOGLE_SHEET_NAME)


def _ensure_tabs(spreadsheet: gspread.Spreadsheet, tickers: list[str]):
    """Create any missing tabs; delete stale ones that no longer match the effective ticker list."""
    existing   = {ws.title: ws for ws in spreadsheet.worksheets()}
    wanted     = set(STATIC_TABS + tickers)

    stale = [t for t in existing if t not in wanted]
    for title in stale:
        spreadsheet.del_worksheet(existing[title])
        log.info("Deleted stale tab: %s", title)

    for tab in STATIC_TABS + tickers:
        if tab not in existing:
            spreadsheet.add_worksheet(title=tab, rows=500, cols=30)
            log.info("Created tab: %s", tab)


def _sanitize_rows(rows: list[list]) -> list[list]:
    """Replace NaN/inf floats with empty string so JSON serialization doesn't blow up."""
    import math
    def _clean(v):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return ""
        return v
    return [[_clean(cell) for cell in row] for row in rows]


def _write_tab(spreadsheet: gspread.Spreadsheet, title: str, rows: list[list]):
    ws = spreadsheet.worksheet(title)
    rows = _sanitize_rows(rows)
    for attempt in range(4):
        try:
            ws.clear()
            if rows:
                ws.update("A1", rows, value_input_option="USER_ENTERED")
            log.info("Written %d rows to '%s'", len(rows), title)
            return
        except gspread.exceptions.APIError as e:
            if "429" in str(e) and attempt < 3:
                wait = 20 * (attempt + 1)   # 20s, 40s, 60s
                log.warning("Sheets quota hit — retrying '%s' in %ds (attempt %d/3)", title, wait, attempt + 1)
                time.sleep(wait)
            else:
                raise


# ── Indicator computation ─────────────────────────────────────────────────────

def _build_df(ticker: str, days: int = 250) -> pd.DataFrame | None:
    candles = get_candles(ticker, days=days)
    if len(candles) < 30:
        return None
    df = pd.DataFrame(candles)
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(float)
    return df


def _safe(val) -> float | str:
    try:
        f = float(val)
        return round(f, 4) if not (np.isnan(f) or np.isinf(f)) else ""
    except (TypeError, ValueError):
        return ""


def _safe_num(val) -> float | None:
    """Return float or None — never raises, never returns NaN/Inf. Used by scoring logic."""
    try:
        f = float(val)
        return f if not (np.isnan(f) or np.isinf(f)) else None
    except (TypeError, ValueError):
        return None


def _fmt_optional(val, decimals: int = 2):
    """None → '', else round to given decimals."""
    if val is None:
        return ""
    return round(float(val), decimals)


def _fmt_pct(val) -> str:
    """Decimal ratio → percentage string: 0.153 → '15.3%'. None → ''."""
    if val is None:
        return ""
    return f"{float(val) * 100:.1f}%"


def _compute_indicators(df: pd.DataFrame) -> dict:
    rsi    = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    macd_i = ta.trend.MACD(df["close"])
    bb     = ta.volatility.BollingerBands(df["close"], window=20)
    ema20  = ta.trend.EMAIndicator(df["close"], window=20).ema_indicator()
    ema50  = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    ema200 = ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()
    atr    = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    vol_sma = df["volume"].rolling(20).mean()

    period = 9
    hh = df["high"].rolling(period).max()
    ll = df["low"].rolling(period).min()
    hl  = (hh - ll).replace(0, 0.001)
    val = (2 * ((df["close"] - ll) / hl) - 1).clip(-0.999, 0.999)
    fisher_vals = 0.5 * np.log((1 + val) / (1 - val))
    fisher_sig  = fisher_vals.shift(1)

    sma_val   = vol_sma.iloc[-1]
    vol_ratio = round(float(df["volume"].iloc[-1] / sma_val), 4) if sma_val and sma_val > 0 else 1.0
    roc5      = df["close"].pct_change(5).iloc[-1]
    roc20     = df["close"].pct_change(20).iloc[-1]

    ind = {
        "rsi":           _safe(rsi.iloc[-1]),
        "macd":          _safe(macd_i.macd().iloc[-1]),
        "macd_signal":   _safe(macd_i.macd_signal().iloc[-1]),
        "macd_hist":     _safe(macd_i.macd_diff().iloc[-1]),
        "bb_upper":      _safe(bb.bollinger_hband().iloc[-1]),
        "bb_lower":      _safe(bb.bollinger_lband().iloc[-1]),
        "bb_middle":     _safe(bb.bollinger_mavg().iloc[-1]),
        "ema20":         _safe(ema20.iloc[-1]),
        "ema50":         _safe(ema50.iloc[-1]),
        "ema200":        _safe(ema200.iloc[-1]),
        "atr":           _safe(atr.iloc[-1]),
        "volume_sma20":  _safe(vol_sma.iloc[-1]),
        "volume_ratio":  vol_ratio,
        "fisher":        _safe(fisher_vals.iloc[-1]),
        "fisher_signal": _safe(fisher_sig.iloc[-1]),
        "roc_5":         _safe(roc5),
        "roc_20":        _safe(roc20),
    }

    e20, e50, e200 = ind["ema20"], ind["ema50"], ind["ema200"]
    close = float(df["close"].iloc[-1])
    if all(isinstance(v, float) for v in [e20, e50, e200]):
        if   close > e20 > e50 > e200:  ind["trend"] = "Strong uptrend"
        elif close > e50 > e200:         ind["trend"] = "Uptrend"
        elif close < e20 < e50 < e200:  ind["trend"] = "Strong downtrend"
        elif close < e50 < e200:         ind["trend"] = "Downtrend"
        else:                            ind["trend"] = "Neutral"
    else:
        ind["trend"] = "N/A"

    return ind


def _compute_composite_signal(
    ind: dict,
    close: float,
    fund: dict | None = None,
    weights: dict | None = None,
    regime: str | None = None,
) -> dict:
    """
    Multi-factor scored signal. When weights (IC-based) are provided, uses adaptive
    JP Morgan-style IC weighting + Goldman-style regime overlay. Falls back to fixed
    weights when weights=None (e.g. logbook rows without per-ticker history).

    Returns {"score": float, "label": str, "reason": str, "regime": str, "top_signal": str}
    Labels: Strong Buy / Buy / Hold / Sell / Strong Sell
    """
    if not ind:
        return {"score": 0, "label": "N/A", "reason": "No data", "regime": regime or "", "top_signal": ""}

    # ── IC-weighted path ──────────────────────────────────────────────────────
    if weights:
        IC_SIGNALS = ["rsi", "macd_hist", "ema_score", "fisher", "bb_position", "roc_5", "roc_20"]
        raw_score = sum(
            _signal_direction(sig, ind, close) * weights.get(sig, 0.0)
            for sig in IC_SIGNALS
        )
        score = raw_score * 5.0   # scale [-1,1] → [-5,5]

        # Fundamentals — additive, not IC-weighted
        if fund:
            rev_growth = fund.get("revenue_growth")
            de_ratio   = fund.get("debt_to_equity")
            if isinstance(rev_growth, float):
                if   rev_growth > 0.15: score += 0.25
                elif rev_growth < 0:    score -= 0.25
            if isinstance(de_ratio, float) and de_ratio > 2.0:
                score -= 0.25

        # Earnings risk penalty
        next_e = (fund or {}).get("next_earnings_date", "")
        if next_e:
            try:
                days_to = (datetime.strptime(next_e, "%Y-%m-%d").date()
                           - datetime.now(timezone.utc).date()).days
                if 0 <= days_to <= 5:
                    score *= 0.7
            except Exception:
                pass

        score = round(score, 2)
        if   score >= 3.5:  label = "Strong Buy"
        elif score >= 1.5:  label = "Buy"
        elif score >= -1.5: label = "Hold"
        elif score >= -3.5: label = "Sell"
        else:               label = "Strong Sell"

        top_sig = max(weights, key=lambda k: abs(weights[k]), default="")
        top_ic  = weights.get(top_sig, 0)
        top_signal_str = f"{top_sig} IC={top_ic:+.2f}" if top_sig else ""
        reason  = (f"Regime:{regime or 'N/A'}  Score {score:+.2f}  "
                   f"Top signal: {top_signal_str}")

        # ── Trend confirmation gate ───────────────────────────────────────────
        # Prevents IC mean-reversion signals from labelling a clearly trending
        # stock as Sell/Buy. If ROC5, ROC20 and EMA trend all agree on direction,
        # cap the label at "Hold" when IC score disagrees — avoids Type II errors.
        roc5  = ind.get("roc_5")
        roc20 = ind.get("roc_20")
        trend = ind.get("trend", "Neutral")
        # "Strong uptrend" (price > EMA20 > EMA50 > EMA200) with positive 20d ROC
        # is sufficient on its own. Regular "Uptrend" requires both ROCs to agree.
        uptrend_confirmed = (
            isinstance(roc20, float) and roc20 > 0 and (
                trend == "Strong uptrend" or
                (trend == "Uptrend" and isinstance(roc5, float) and roc5 > 0)
            )
        )
        downtrend_confirmed = (
            isinstance(roc20, float) and roc20 < 0 and (
                trend == "Strong downtrend" or
                (trend == "Downtrend" and isinstance(roc5, float) and roc5 < 0)
            )
        )
        if uptrend_confirmed and score < -1.5:
            score = -1.4
            label = "Hold"
            reason += "  [↑ trend gate]"
        elif downtrend_confirmed and score > 1.5:
            score = 1.4
            label = "Hold"
            reason += "  [↓ trend gate]"

        return {"score": score, "label": label, "reason": reason,
                "regime": regime or "", "top_signal": top_signal_str}

    # ── Legacy fixed-weight path (unchanged) ─────────────────────────────────
    score = 0.0
    parts = []

    score = 0.0
    parts = []

    # ── EMA alignment (weight ±2) ─────────────────────────────────────────────
    trend = ind.get("trend", "Neutral")
    trend_scores = {
        "Strong uptrend":   ( 2.0, "EMA strong uptrend (+2)"),
        "Uptrend":          ( 1.0, "EMA uptrend (+1)"),
        "Strong downtrend": (-2.0, "EMA strong downtrend (-2)"),
        "Downtrend":        (-1.0, "EMA downtrend (-1)"),
    }
    if trend in trend_scores:
        s, label = trend_scores[trend]
        score += s
        parts.append(label)

    # ── RSI (weight ±1, contrarian at extremes) ───────────────────────────────
    rsi = ind.get("rsi")
    if isinstance(rsi, float):
        if rsi >= 70:
            score -= 1.0;  parts.append(f"RSI {rsi:.0f} overbought (-1)")
        elif rsi >= 60:
            score += 0.5;  parts.append(f"RSI {rsi:.0f} bullish momentum (+0.5)")
        elif rsi <= 30:
            score += 1.0;  parts.append(f"RSI {rsi:.0f} oversold/contrarian buy (+1)")
        elif rsi <= 40:
            score -= 0.5;  parts.append(f"RSI {rsi:.0f} weakening (-0.5)")

    # ── MACD line vs signal (±1) + histogram sign (±0.25) ────────────────────
    macd, msig, mhist = ind.get("macd"), ind.get("macd_signal"), ind.get("macd_hist")
    if isinstance(macd, float) and isinstance(msig, float):
        if macd > msig:
            score += 1.0;  parts.append("MACD above signal (+1)")
        else:
            score -= 1.0;  parts.append("MACD below signal (-1)")
        if isinstance(mhist, float):
            if mhist > 0:
                score += 0.25; parts.append("MACD hist positive (+0.25)")
            elif mhist < 0:
                score -= 0.25; parts.append("MACD hist negative (-0.25)")

    # ── Fisher Transform (±0.5) ───────────────────────────────────────────────
    fisher, fsig = ind.get("fisher"), ind.get("fisher_signal")
    if isinstance(fisher, float) and isinstance(fsig, float):
        if fisher > fsig:
            score += 0.5;  parts.append("Fisher bullish (+0.5)")
        else:
            score -= 0.5;  parts.append("Fisher bearish (-0.5)")

    # ── Bollinger Band position (±0.5) ────────────────────────────────────────
    bb_upper = ind.get("bb_upper")
    bb_lower = ind.get("bb_lower")
    bb_mid   = ind.get("bb_middle")
    if all(isinstance(v, float) for v in [bb_upper, bb_lower, bb_mid]) and close:
        if close > bb_upper:
            score -= 0.5;  parts.append("Above BB upper — overextended (-0.5)")
        elif close < bb_lower:
            score += 0.5;  parts.append("Below BB lower — oversold bounce (+0.5)")
        elif close > bb_mid:
            score += 0.25; parts.append("In upper BB half (+0.25)")
        else:
            score -= 0.25; parts.append("In lower BB half (-0.25)")

    # ── Fundamental modifiers (optional) ─────────────────────────────────────
    if fund:
        rev_growth = fund.get("revenue_growth")
        de_ratio   = fund.get("debt_to_equity")
        if isinstance(rev_growth, float):
            if rev_growth > 0.15:
                score += 0.25; parts.append(f"Rev growth {rev_growth*100:.0f}% (+0.25)")
            elif rev_growth < 0:
                score -= 0.25; parts.append(f"Rev growth {rev_growth*100:.0f}% (-0.25)")
        if isinstance(de_ratio, float) and de_ratio > 2.0:
            score -= 0.25; parts.append(f"D/E {de_ratio:.1f} high debt (-0.25)")

    # ── Volume multiplier [0.75, 1.5] — amplifies or dampens the signal ──────
    vol_ratio = ind.get("volume_ratio", 1.0)
    if isinstance(vol_ratio, float) and vol_ratio > 0:
        multiplier = max(0.75, min(1.5, vol_ratio))
        score *= multiplier

    # ── Label from thresholds ─────────────────────────────────────────────────
    if   score >= 3.5:  label = "Strong Buy"
    elif score >= 1.5:  label = "Buy"
    elif score >= -1.5: label = "Hold"
    elif score >= -3.5: label = "Sell"
    else:               label = "Strong Sell"

    reason = f"Score {score:+.2f} | " + " · ".join(parts) if parts else f"Score {score:+.2f}"
    return {"score": round(score, 2), "label": label, "reason": reason, "regime": "", "top_signal": ""}


def _signal_reason(ind: dict, ticker: str) -> str:
    parts = [ticker]
    e20, e50, e200 = ind.get("ema20"), ind.get("ema50"), ind.get("ema200")
    rsi = ind.get("rsi")
    vr  = ind.get("volume_ratio", 1.0)
    macd, msig   = ind.get("macd"),   ind.get("macd_signal")
    fisher, fsig = ind.get("fisher"), ind.get("fisher_signal")

    if isinstance(e50, float) and isinstance(e200, float):
        if isinstance(e20, float) and e20 > e50 > e200:
            parts.append("Above EMA20/50/200")
        elif e50 > e200:
            parts.append("Above EMA50/200")
        elif e50 < e200:
            parts.append("Below EMA50/200")

    if isinstance(rsi, float):
        if   rsi >= 70: parts.append(f"RSI {rsi:.0f} (overbought)")
        elif rsi >= 60: parts.append(f"RSI {rsi:.0f} (momentum building)")
        elif rsi <= 30: parts.append(f"RSI {rsi:.0f} (oversold)")
        elif rsi <= 40: parts.append(f"RSI {rsi:.0f} (weakening)")
        else:           parts.append(f"RSI {rsi:.0f} (neutral)")

    if isinstance(vr, float):
        if   vr >= 1.5: parts.append(f"Volume {vr:.1f}× avg (high)")
        elif vr <= 0.7: parts.append(f"Volume {vr:.1f}× avg (low)")
        else:           parts.append(f"Volume {vr:.1f}× avg")

    if isinstance(fisher, float) and isinstance(fsig, float):
        parts.append("Fisher bullish" if fisher > fsig else "Fisher bearish")

    if isinstance(macd, float) and isinstance(msig, float):
        parts.append("MACD bullish" if macd > msig else "MACD bearish")

    return ": ".join([parts[0], " · ".join(parts[1:])]) if len(parts) > 1 else ticker


# ── Market sentiment narrative ────────────────────────────────────────────────

def _market_sentiment(ind: dict, close: float, day_change_pct) -> str:
    """Return a 2-3 sentence plain-English sentiment summary derived from indicators."""
    trend        = ind.get("trend", "Neutral")
    rsi          = ind.get("rsi")
    macd         = ind.get("macd")
    macd_signal  = ind.get("macd_signal")
    bb_upper     = ind.get("bb_upper")
    bb_lower     = ind.get("bb_lower")
    bb_middle    = ind.get("bb_middle")
    volume_ratio = ind.get("volume_ratio", 1.0)
    fisher       = ind.get("fisher")
    fisher_sig   = ind.get("fisher_signal")

    parts = []

    # Trend
    trend_map = {
        "Strong uptrend":   "Price is trading above all major EMAs (20/50/200), confirming a strong uptrend.",
        "Uptrend":          "Price is above EMA50 and EMA200, indicating an established uptrend.",
        "Strong downtrend": "Price is below all major EMAs (20/50/200), confirming a strong downtrend.",
        "Downtrend":        "Price is below EMA50 and EMA200, reflecting a sustained downtrend.",
        "Neutral":          "Price is oscillating around the key EMAs with no clear directional trend.",
    }
    parts.append(trend_map.get(trend, "Trend is unclear."))

    # Momentum (RSI + MACD)
    if isinstance(rsi, float):
        if rsi >= 70:
            momentum = f"RSI at {rsi:.0f} signals overbought conditions — upside momentum may be exhausting."
        elif rsi >= 60:
            momentum = f"RSI at {rsi:.0f} reflects building bullish momentum without being overbought."
        elif rsi <= 30:
            momentum = f"RSI at {rsi:.0f} signals oversold conditions — a mean-reversion bounce may be near."
        elif rsi <= 40:
            momentum = f"RSI at {rsi:.0f} shows weakening momentum with bearish pressure building."
        else:
            momentum = f"RSI at {rsi:.0f} is neutral, offering no strong directional signal."
        if isinstance(macd, float) and isinstance(macd_signal, float):
            macd_note = "MACD supports this with a bullish crossover." if macd > macd_signal else "MACD is below its signal line, adding to downside pressure."
            parts.append(f"{momentum} {macd_note}")
        else:
            parts.append(momentum)

    # Volatility / BB position
    if all(isinstance(v, float) for v in [bb_upper, bb_lower, bb_middle]) and close:
        bb_width = bb_upper - bb_lower
        if close > bb_upper:
            parts.append("Price is above the upper Bollinger Band — the move may be overextended in the short term.")
        elif close < bb_lower:
            parts.append("Price is below the lower Bollinger Band — the stock may be in breakdown or deeply oversold.")
        elif close > bb_middle:
            parts.append(f"Price is in the upper half of the Bollinger Bands (width ${bb_width:.2f}), suggesting controlled bullish momentum.")
        else:
            parts.append(f"Price is in the lower half of the Bollinger Bands (width ${bb_width:.2f}), suggesting caution.")

    # Volume conviction
    if isinstance(volume_ratio, float):
        if volume_ratio >= 1.5:
            parts.append(f"Volume at {volume_ratio:.1f}× the 20-day average signals strong market participation behind the move.")
        elif volume_ratio <= 0.7:
            parts.append(f"Volume is thin at {volume_ratio:.1f}× average — weak conviction; the move may lack follow-through.")

    # Day context
    try:
        chg = float(day_change_pct)
        if chg >= 4:
            parts.append(f"A strong {chg:.1f}% session gain adds near-term bullish bias.")
        elif chg <= -4:
            parts.append(f"A sharp {abs(chg):.1f}% session decline adds near-term bearish bias.")
    except (TypeError, ValueError):
        pass

    # Fisher cycle signal
    if isinstance(fisher, float) and isinstance(fisher_sig, float):
        if fisher > fisher_sig:
            parts.append("Fisher Transform shows a bullish cycle crossover — price may be entering an upswing phase.")
        elif fisher < fisher_sig:
            parts.append("Fisher Transform shows a bearish cycle crossover — price may be entering a downswing phase.")

    return "  ".join(parts[:4])   # cap at 4 sentences to keep the cell readable


# ── Reversal detection ────────────────────────────────────────────────────────

def _compute_reversal_signal(ind: dict, close: float) -> dict:
    """
    Multi-factor reversal detector. 5 indicators each cast a bullish or bearish vote.
    Returns {label, bull_score, bear_score, direction, reason}.

    Labels: "Bullish Reversal" (bull≥3), "Bullish Watch" (bull==2),
            "Bearish Reversal" (bear≥3), "Bearish Watch" (bear==2), "No Signal".
    """
    bull = 0
    bear = 0
    reasons: list[str] = []

    rsi = ind.get("rsi")
    if isinstance(rsi, float):
        if rsi < 32:
            bull += 1; reasons.append(f"RSI {rsi:.0f} oversold")
        elif rsi > 68:
            bear += 1; reasons.append(f"RSI {rsi:.0f} overbought")

    bbu, bbl = ind.get("bb_upper"), ind.get("bb_lower")
    if isinstance(bbu, float) and isinstance(bbl, float) and close:
        if close < bbl:
            bull += 1; reasons.append("Below BB lower")
        elif close > bbu:
            bear += 1; reasons.append("Above BB upper")

    fisher, fsig = ind.get("fisher"), ind.get("fisher_signal")
    if isinstance(fisher, float) and isinstance(fsig, float):
        if fisher < -2.0 and fisher > fsig:
            bull += 1; reasons.append(f"Fisher {fisher:.2f} bullish cross")
        elif fisher > 2.0 and fisher < fsig:
            bear += 1; reasons.append(f"Fisher {fisher:.2f} bearish cross")

    mh = ind.get("macd_hist")
    if isinstance(mh, float):
        if mh > 0:
            bull += 1; reasons.append("MACD hist positive")
        elif mh < 0:
            bear += 1; reasons.append("MACD hist negative")

    e50 = ind.get("ema50")
    if isinstance(e50, float) and e50 > 0 and close:
        dev = (close - e50) / e50
        if dev < -0.08:
            bull += 1; reasons.append(f"EMA50 dev {dev*100:.1f}%")
        elif dev > 0.08:
            bear += 1; reasons.append(f"EMA50 dev {dev*100:.1f}%")

    if bull >= 3:
        label = "Bullish Reversal"
    elif bull == 2:
        label = "Bullish Watch"
    elif bear >= 3:
        label = "Bearish Reversal"
    elif bear == 2:
        label = "Bearish Watch"
    else:
        label = "No Signal"

    direction = "Bullish" if bull > bear else ("Bearish" if bear > bull else "Neutral")
    return {
        "label":      label,
        "bull_score": bull,
        "bear_score": bear,
        "direction":  direction,
        "reason":     " · ".join(reasons[:3]),
    }


# ── Tab: Overview ─────────────────────────────────────────────────────────────

def _export_overview(
    spreadsheet: gspread.Spreadsheet,
    positions: dict,
    fundamentals: dict,
    tickers: list[str],
    widget_rows: list[list],
    ticker_weights: dict,
    regime: str,
):
    headers = [
        "Ticker", "Date", "Close", "Open", "High", "Low", "Volume",
        "Day Change %",
        "Signal Label", "Signal Score", "Regime", "Top Signal",
        "RSI", "MACD", "MACD Signal", "MACD Hist",
        "EMA20", "EMA50", "EMA200",
        "BB Upper", "BB Middle", "BB Lower",
        "ATR", "Volume Ratio", "Fisher", "Fisher Signal",
        "ROC 5d", "ROC 20d",
        "Signal Reason",
        "Direction", "Total Units", "Avg Entry", "# Legs",
        "Total Cost", "Current Value", "Unrealized P&L", "P&L %",
        "Day P&L $", "Day P&L %",
        "P/E", "Fwd P/E", "EPS TTM", "Rev Growth %", "Profit Margin %",
        "Beta", "Analyst Target",
    ]
    portfolio = {r["ticker"]: r for r in get_portfolio_summary()}
    ticker_rows = []

    for ticker in tickers:
        p     = portfolio.get(ticker, {})
        df    = _build_df(ticker)
        ind   = _compute_indicators(df) if df is not None else {}
        legs  = positions.get(ticker, [])
        close = float(p.get("latest_close") or 0)
        agg   = _aggregate_pnl(legs, close) if legs and close else {}
        fund  = fundamentals.get(ticker, {})
        w     = ticker_weights.get(ticker)
        comp  = _compute_composite_signal(ind, close, fund, weights=w, regime=regime)

        prev_close = float(p.get("prev_close") or 0)
        if prev_close and agg:
            day_pnl     = round((close - prev_close) * agg["total_units"], 2)
            day_pnl_pct = round((close - prev_close) / prev_close * 100, 2)
        else:
            day_pnl = day_pnl_pct = ""

        ticker_rows.append([
            ticker,
            p.get("latest_date", ""),
            p.get("latest_close", ""),
            p.get("latest_open", ""),
            p.get("latest_high", ""),
            p.get("latest_low", ""),
            p.get("latest_volume", ""),
            p.get("day_change_pct", ""),
            comp["label"],
            comp["score"],
            comp.get("regime", regime),
            comp.get("top_signal", ""),
            ind.get("rsi", ""),
            ind.get("macd", ""),
            ind.get("macd_signal", ""),
            ind.get("macd_hist", ""),
            ind.get("ema20", ""),
            ind.get("ema50", ""),
            ind.get("ema200", ""),
            ind.get("bb_upper", ""),
            ind.get("bb_middle", ""),
            ind.get("bb_lower", ""),
            ind.get("atr", ""),
            ind.get("volume_ratio", ""),
            ind.get("fisher", ""),
            ind.get("fisher_signal", ""),
            ind.get("roc_5", ""),
            ind.get("roc_20", ""),
            comp["reason"],
            agg.get("direction", ""),
            agg.get("total_units", ""),
            agg.get("avg_entry", ""),
            len(legs) if legs else "",
            agg.get("total_cost", ""),
            agg.get("total_value", ""),
            agg.get("total_pnl", ""),
            agg.get("pnl_pct", ""),
            day_pnl,
            day_pnl_pct,
            _fmt_optional(fund.get("pe_ratio")),
            _fmt_optional(fund.get("forward_pe")),
            _fmt_optional(fund.get("eps_ttm")),
            _fmt_pct(fund.get("revenue_growth")),
            _fmt_pct(fund.get("profit_margin")),
            _fmt_optional(fund.get("beta")),
            _fmt_optional(fund.get("analyst_target")),
        ])

    # Widget block (rows 1–8) + spacer (row 9) + headers (row 10) + data (row 11+)
    all_rows = widget_rows + [[""]] + [headers] + ticker_rows
    _write_tab(spreadsheet, TAB_OVERVIEW, all_rows)


# ── Tab: Live Overview ───────────────────────────────────────────────────────

def _export_live_overview(
    spreadsheet: gspread.Spreadsheet,
    positions: dict,
    fundamentals: dict,
    tickers: list[str],
    portfolio: dict,
    widget_rows: list[list],
    ticker_weights: dict,
    regime: str,
):
    """
    Live dashboard updating on every run (hourly + full).
    Widget block (rows 1–8) + per-ticker table with signal + reversal columns.
    BB Position: 0.0 = at lower band, 1.0 = at upper band.
    """
    headers = [
        "Ticker", "Live Price", "Day Change %", "Day P&L $",
        "Total P&L $", "Total P&L %",
        "Signal Label", "Signal Score", "Regime", "Top Signal",
        "Reversal Label", "Bull Votes", "Bear Votes", "Reversal Reason",
        "RSI", "Fisher", "BB Position", "MACD Hist", "EMA50 Dev %",
    ]

    ticker_rows = []
    for ticker in tickers:
        p     = portfolio.get(ticker, {})
        df    = _build_df(ticker)
        ind   = _compute_indicators(df) if df is not None else {}
        legs  = positions.get(ticker, [])
        close = float(p.get("latest_close") or 0)
        agg   = _aggregate_pnl(legs, close) if legs and close else {}
        fund  = fundamentals.get(ticker, {})
        w     = ticker_weights.get(ticker)
        comp  = _compute_composite_signal(ind, close, fund, weights=w, regime=regime)
        rev   = _compute_reversal_signal(ind, close)

        prev_close = float(p.get("prev_close") or 0)
        if prev_close and agg:
            day_pnl = round((close - prev_close) * agg["total_units"], 2)
        else:
            day_pnl = ""

        # BB Position: 0 = at lower band, 1 = at upper band
        bbu, bbl = ind.get("bb_upper"), ind.get("bb_lower")
        bb_pos = ""
        if isinstance(bbu, float) and isinstance(bbl, float) and bbu != bbl and close:
            bb_pos = round((close - bbl) / (bbu - bbl), 3)

        # EMA50 deviation %
        e50 = ind.get("ema50")
        ema50_dev = ""
        if isinstance(e50, float) and e50 > 0 and close:
            ema50_dev = round((close - e50) / e50 * 100, 2)

        ticker_rows.append([
            ticker,
            close or "",
            p.get("day_change_pct", ""),
            day_pnl,
            agg.get("total_pnl", "") if agg else "",
            agg.get("pnl_pct", "")   if agg else "",
            comp["label"],
            comp["score"],
            comp.get("regime", regime),
            comp.get("top_signal", ""),
            rev["label"],
            rev["bull_score"],
            rev["bear_score"],
            rev["reason"],
            ind.get("rsi", ""),
            ind.get("fisher", ""),
            bb_pos,
            ind.get("macd_hist", ""),
            ema50_dev,
        ])

    all_rows = widget_rows + [[""]] + [headers] + ticker_rows
    _write_tab(spreadsheet, TAB_LIVE, all_rows)


# ── Tab: Per-stock ────────────────────────────────────────────────────────────

def _export_stock_tab(
    spreadsheet: gspread.Spreadsheet,
    ticker: str,
    portfolio: dict,
    positions: dict,
    fundamentals: dict,
    weights: dict | None = None,
    regime: str | None = None,
):
    p       = portfolio.get(ticker, {})
    df      = _build_df(ticker, days=250)
    ind     = _compute_indicators(df) if df is not None else {}
    legs    = positions.get(ticker, [])
    close   = float(p.get("latest_close") or 0)
    agg     = _aggregate_pnl(legs, close) if legs and close else {}
    day_chg = p.get("day_change_pct", "")
    fund    = fundamentals.get(ticker, {})
    comp    = _compute_composite_signal(ind, close, fund, weights=weights, regime=regime)

    rows = []

    # ── Section 0: Company ────────────────────────────────────────────────────
    rows.append(["COMPANY", ""])
    rows.append(["About", COMPANY_INFO.get(ticker, ticker)])
    rows.append(["", ""])

    # ── Section 1: Snapshot ───────────────────────────────────────────────────
    rows.append(["SNAPSHOT", ""])
    rows.append(["Ticker",        ticker])
    rows.append(["Date",          p.get("latest_date", "")])
    rows.append(["Close",         close])
    rows.append(["Open",          p.get("latest_open", "")])
    rows.append(["High",          p.get("latest_high", "")])
    rows.append(["Low",           p.get("latest_low", "")])
    rows.append(["Volume",        p.get("latest_volume", "")])
    rows.append(["Day Change %",  day_chg])
    rows.append(["Prev Close",    p.get("prev_close", "")])
    rows.append(["Signal Label",  comp["label"]])
    rows.append(["Signal Score",  comp["score"]])
    rows.append(["Signal Reason", comp["reason"]])
    rows.append(["", ""])

    # ── Section 1b: Signal Weights ────────────────────────────────────────────
    rows.append(["SIGNAL WEIGHTS", ""])
    rows.append(["Regime", regime or "N/A"])
    if weights:
        rows.append(["Signal", "IC Weight", "Direction"])
        for sig_label, sig_key in [
            ("RSI (14)",           "rsi"),
            ("MACD Histogram",     "macd_hist"),
            ("EMA Score",          "ema_score"),
            ("Fisher Transform",   "fisher"),
            ("BB Position",        "bb_position"),
            ("Rate of Change 5d",  "roc_5"),
            ("Rate of Change 20d", "roc_20"),
        ]:
            w_val = weights.get(sig_key, 0.0)
            dir_str = "Bullish" if w_val > 0 else ("Bearish" if w_val < 0 else "Neutral")
            rows.append([sig_label, f"{w_val:+.4f}", dir_str])
        rows.append(["Top Signal", comp.get("top_signal", ""), ""])
    else:
        rows.append(["Insufficient history for IC weights (need ≥40 candles)", "", ""])
    rows.append(["", ""])

    # ── Section 2: Market Sentiment ───────────────────────────────────────────
    rows.append(["MARKET SENTIMENT & CONDITION", ""])
    rows.append(["Analysis", _market_sentiment(ind, close, day_chg) if ind else "Insufficient data."])
    rows.append(["", ""])

    # ── Section 3: Fundamentals ───────────────────────────────────────────────
    rows.append(["FUNDAMENTALS", ""])
    rows.append(["P/E Ratio (TTM)",    _fmt_optional(fund.get("pe_ratio"))])
    rows.append(["Forward P/E",        _fmt_optional(fund.get("forward_pe"))])
    rows.append(["EPS TTM",            _fmt_optional(fund.get("eps_ttm"))])
    rows.append(["Revenue Growth YoY", _fmt_pct(fund.get("revenue_growth"))])
    rows.append(["Profit Margin",      _fmt_pct(fund.get("profit_margin"))])
    rows.append(["Debt / Equity",      _fmt_optional(fund.get("debt_to_equity"))])
    rows.append(["Beta",               _fmt_optional(fund.get("beta"))])
    rows.append(["Analyst Target",     _fmt_optional(fund.get("analyst_target"))])
    rows.append(["Next Earnings",      fund.get("next_earnings_date", "")])
    rows.append(["", ""])

    # ── Section 4: Recent News ────────────────────────────────────────────────
    rows.append(["RECENT NEWS", ""])
    news_items = fund.get("recent_news", [])
    if news_items:
        for item in news_items:
            rows.append([item.get("date", ""), item.get("title", "")])
    else:
        rows.append(["No recent news available", ""])
    rows.append(["", ""])

    # ── Section 5: Open Positions ─────────────────────────────────────────────
    rows.append(["OPEN POSITIONS", ""])
    if agg:
        # Aggregate summary
        rows.append(["── AGGREGATE ──", ""])
        rows.append(["Direction",      agg.get("direction", "")])
        rows.append(["Total Units",    agg.get("total_units", "")])
        rows.append(["Avg Entry Price",agg.get("avg_entry", "")])
        rows.append(["Total Cost",     agg.get("total_cost", "")])
        rows.append(["Current Value",  agg.get("total_value", "")])
        rows.append(["Unrealized P&L", agg.get("total_pnl", "")])
        rows.append(["P&L %",          agg.get("pnl_pct", "")])
        rows.append(["", ""])
        # Individual legs
        rows.append(["── INDIVIDUAL LEGS ──", "", "", "", "", "", "", ""])
        rows.append(["#", "Direction", "Units", "Entry Price", "Open Date", "Stop Loss", "Take Profit", "Cost Basis", "Current Value", "P&L $", "P&L %"])
        for i, leg in enumerate(agg.get("legs", []), 1):
            rows.append([
                i,
                leg.get("direction", ""),
                leg.get("units", ""),
                leg.get("open_price", ""),
                leg.get("open_date", ""),
                leg.get("stop_loss", ""),
                leg.get("take_profit", ""),
                leg.get("cost_basis", ""),
                leg.get("current_value", ""),
                leg.get("unrealized_pnl", ""),
                leg.get("pnl_pct", ""),
            ])
    else:
        rows.append(["No open positions — fill in positions.json to track P&L", ""])
    rows.append(["", ""])

    # ── Section 6: Indicators ─────────────────────────────────────────────────
    rows.append(["INDICATORS", ""])
    rows.append(["RSI (14)",      ind.get("rsi", "")])
    rows.append(["MACD",          ind.get("macd", "")])
    rows.append(["MACD Signal",   ind.get("macd_signal", "")])
    rows.append(["MACD Hist",     ind.get("macd_hist", "")])
    rows.append(["EMA 20",        ind.get("ema20", "")])
    rows.append(["EMA 50",        ind.get("ema50", "")])
    rows.append(["EMA 200",       ind.get("ema200", "")])
    rows.append(["BB Upper",      ind.get("bb_upper", "")])
    rows.append(["BB Middle",     ind.get("bb_middle", "")])
    rows.append(["BB Lower",      ind.get("bb_lower", "")])
    rows.append(["ATR (14)",      ind.get("atr", "")])
    rows.append(["Volume SMA 20", ind.get("volume_sma20", "")])
    rows.append(["Volume Ratio",  ind.get("volume_ratio", "")])
    rows.append(["Fisher",        ind.get("fisher", "")])
    rows.append(["Fisher Signal", ind.get("fisher_signal", "")])
    rows.append(["", ""])

    # ── Section 7: OHLCV history ──────────────────────────────────────────────
    rows.append(["OHLCV HISTORY (90 days)", "", "", "", "", ""])
    rows.append(["Date", "Open", "High", "Low", "Close", "Volume", "Change %"])

    candles = get_candles(ticker, days=90)
    for i, c in enumerate(candles):
        prev_close = candles[i - 1]["close"] if i > 0 else None
        chg = round((c["close"] - prev_close) / prev_close * 100, 2) if prev_close else ""
        rows.append([c["date"], c["open"], c["high"], c["low"], c["close"], c["volume"], chg])

    _write_tab(spreadsheet, ticker, rows)


# ── yfinance fundamentals ─────────────────────────────────────────────────────

def _fetch_next_earnings(yf_ticker) -> str:
    """Return next earnings date as 'YYYY-MM-DD', or '' if unavailable."""
    try:
        cal = yf_ticker.calendar
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date") or cal.get("Earnings Dates") or []
            if dates:
                return str(dates[0])[:10]
        elif hasattr(cal, "empty") and not cal.empty:
            col = cal.get("Earnings Date") if "Earnings Date" in cal else None
            if col is not None:
                return str(col.iloc[0])[:10]
    except Exception:
        pass
    return ""


def _fetch_recent_news(yf_ticker, n: int = 3) -> list[dict]:
    """Return up to n recent news items as [{"title": str, "date": str}]."""
    try:
        news = yf_ticker.news or []
        result = []
        for item in news[:n]:
            title = item.get("title", "")
            ts    = item.get("providerPublishTime")
            date_str = ""
            if ts:
                try:
                    date_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
                except Exception:
                    pass
            result.append({"title": title, "date": date_str})
        return result
    except Exception:
        return []


_EMPTY_FUND = {
    "market_cap": "", "sector": "", "week52_high": "", "week52_low": "",
    "pe_ratio": None, "forward_pe": None, "eps_ttm": None,
    "revenue_growth": None, "profit_margin": None,
    "debt_to_equity": None, "beta": None, "analyst_target": None,
    "next_earnings_date": "", "recent_news": [],
}


def _fetch_fundamentals(tickers: list[str]) -> dict[str, dict]:
    """Fetch market cap, sector, 52W H/L, financials, and recent news for all tickers."""
    result = {}
    try:
        data = yf.Tickers(" ".join(tickers))
        for ticker in tickers:
            try:
                info      = data.tickers[ticker].fast_info
                full_info = data.tickers[ticker].info
                result[ticker] = {
                    # Existing fields (preserved)
                    "market_cap":        _fmt_market_cap(getattr(info, "market_cap", None)),
                    "sector":            full_info.get("sector", ""),
                    "week52_high":       round(float(getattr(info, "year_high", 0) or 0), 2),
                    "week52_low":        round(float(getattr(info, "year_low",  0) or 0), 2),
                    "shares_out":        getattr(info, "shares", None),
                    # Financial ratios
                    "pe_ratio":          _safe_num(full_info.get("trailingPE")),
                    "forward_pe":        _safe_num(full_info.get("forwardPE")),
                    "eps_ttm":           _safe_num(full_info.get("trailingEps")),
                    "revenue_growth":    _safe_num(full_info.get("revenueGrowth")),
                    "profit_margin":     _safe_num(full_info.get("profitMargins")),
                    "debt_to_equity":    _safe_num(full_info.get("debtToEquity")),
                    "beta":              _safe_num(full_info.get("beta")),
                    "analyst_target":    _safe_num(full_info.get("targetMeanPrice")),
                    # Earnings & news
                    "next_earnings_date": _fetch_next_earnings(data.tickers[ticker]),
                    "recent_news":        _fetch_recent_news(data.tickers[ticker], n=3),
                }
            except Exception:
                log.debug("yfinance fetch failed for %s", ticker)
                result[ticker] = dict(_EMPTY_FUND)
    except Exception as e:
        log.warning("yfinance batch fetch failed: %s", e)
        for t in tickers:
            result[t] = dict(_EMPTY_FUND)
    return result


def _fmt_market_cap(val) -> str:
    if not val:
        return ""
    val = float(val)
    if val >= 1e12:   return f"${val/1e12:.2f}T"
    if val >= 1e9:    return f"${val/1e9:.2f}B"
    if val >= 1e6:    return f"${val/1e6:.2f}M"
    return f"${val:,.0f}"


def _candle_type(o, h, l, c) -> str:
    """Classify the daily candle into a simple pattern."""
    try:
        o, h, l, c = float(o), float(h), float(l), float(c)
    except (TypeError, ValueError):
        return ""
    body   = abs(c - o)
    rng    = h - l
    if rng == 0:
        return "Flat"
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    body_pct   = body / rng

    if body_pct < 0.1:
        return "Doji"
    if lower_wick > body * 2 and upper_wick < body * 0.5:
        return "Hammer" if c > o else "Hanging Man"
    if upper_wick > body * 2 and lower_wick < body * 0.5:
        return "Shooting Star" if c < o else "Inverted Hammer"
    return "Bullish" if c >= o else "Bearish"


# ── Tab: Log Book ─────────────────────────────────────────────────────────────

def _export_logbook(spreadsheet: gspread.Spreadsheet, fundamentals: dict, tickers: list[str]):
    headers = [
        # Identity
        "Ticker", "Sector", "Date",
        # Price action
        "Open", "High", "Low", "Close",
        "Day Change $", "Day Change %",
        "True Range", "Gap vs Prev Close",
        "Candle Type",
        # Volume
        "Volume", "Volume Ratio",
        # Market fundamentals
        "Market Cap", "52W High", "52W Low",
        "% from 52W High", "% from 52W Low",
        # Technical snapshot
        "Trend", "RSI", "MACD Direction",
        "EMA20", "EMA50", "EMA200",
        "ATR", "BB Width",
    ]

    rows = [headers]

    for ticker in tickers:
        candles = get_candles(ticker, days=90)
        if not candles:
            continue

        df  = _build_df(ticker, days=250)
        fun = fundamentals.get(ticker, {})

        # Pre-compute indicator series for the full df
        if df is not None and len(df) >= 30:
            rsi_s   = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
            macd_i  = ta.trend.MACD(df["close"])
            ema20_s = ta.trend.EMAIndicator(df["close"], window=20).ema_indicator()
            ema50_s = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
            ema200_s= ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()
            atr_s   = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
            bb      = ta.volatility.BollingerBands(df["close"], window=20)
            vol_sma = df["volume"].rolling(20).mean()
            df["rsi"]        = rsi_s
            df["macd"]       = macd_i.macd()
            df["macd_sig"]   = macd_i.macd_signal()
            df["ema20"]      = ema20_s
            df["ema50"]      = ema50_s
            df["ema200"]     = ema200_s
            df["atr"]        = atr_s
            df["bb_upper"]   = bb.bollinger_hband()
            df["bb_lower"]   = bb.bollinger_lband()
            df["vol_sma"]    = vol_sma
            df_indexed = df.set_index("date")
        else:
            df_indexed = None

        w52h = fun.get("week52_high") or ""
        w52l = fun.get("week52_low")  or ""

        for i, c in enumerate(candles):
            prev        = candles[i - 1] if i > 0 else None
            prev_close  = float(prev["close"]) if prev else None
            close       = float(c["close"])
            open_       = float(c["open"])
            high        = float(c["high"])
            low         = float(c["low"])
            volume      = float(c["volume"]) if c["volume"] else 0

            day_chg_d   = round(close - prev_close, 4)        if prev_close else ""
            day_chg_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else ""
            true_range  = round(high - low, 4)
            gap         = round(open_ - prev_close, 4)        if prev_close else ""

            # Indicator values for this date
            ind_row = {}
            if df_indexed is not None and c["date"] in df_indexed.index:
                r = df_indexed.loc[c["date"]]
                ind_row = {
                    "rsi":      _safe(r.get("rsi")),
                    "macd":     r.get("macd"),
                    "macd_sig": r.get("macd_sig"),
                    "ema20":    _safe(r.get("ema20")),
                    "ema50":    _safe(r.get("ema50")),
                    "ema200":   _safe(r.get("ema200")),
                    "atr":      _safe(r.get("atr")),
                    "bb_upper": r.get("bb_upper"),
                    "bb_lower": r.get("bb_lower"),
                    "vol_sma":  r.get("vol_sma"),
                }

            macd_dir = ""
            if ind_row.get("macd") and ind_row.get("macd_sig"):
                macd_dir = "Bullish" if float(ind_row["macd"]) > float(ind_row["macd_sig"]) else "Bearish"

            vol_ratio = ""
            if ind_row.get("vol_sma") and float(ind_row["vol_sma"]) > 0:
                vol_ratio = round(volume / float(ind_row["vol_sma"]), 2)

            bb_width = ""
            if ind_row.get("bb_upper") and ind_row.get("bb_lower"):
                bb_width = round(float(ind_row["bb_upper"]) - float(ind_row["bb_lower"]), 4)

            e20, e50, e200 = ind_row.get("ema20", ""), ind_row.get("ema50", ""), ind_row.get("ema200", "")
            trend = ""
            if all(isinstance(v, float) for v in [e20, e50, e200]):
                if   close > e20 > e50 > e200:  trend = "Strong uptrend"
                elif close > e50 > e200:         trend = "Uptrend"
                elif close < e20 < e50 < e200:  trend = "Strong downtrend"
                elif close < e50 < e200:         trend = "Downtrend"
                else:                            trend = "Neutral"

            pct_from_52h = round((close - float(w52h)) / float(w52h) * 100, 2) if w52h else ""
            pct_from_52l = round((close - float(w52l)) / float(w52l) * 100, 2) if w52l else ""

            rows.append([
                ticker,
                fun.get("sector", ""),
                c["date"],
                open_, high, low, close,
                day_chg_d, day_chg_pct,
                true_range, gap,
                _candle_type(open_, high, low, close),
                volume, vol_ratio,
                fun.get("market_cap", ""),
                w52h, w52l,
                pct_from_52h, pct_from_52l,
                trend,
                ind_row.get("rsi", ""),
                macd_dir,
                e20, e50, e200,
                ind_row.get("atr", ""),
                bb_width,
            ])

    # Sort by date descending, then ticker
    data_rows = sorted(rows[1:], key=lambda r: (r[2], r[0]), reverse=True)
    _write_tab(spreadsheet, TAB_LOGBOOK, [headers] + data_rows)


# ── Tab: Monthly Performance ─────────────────────────────────────────────────

def _export_monthly_performance(
    spreadsheet: gspread.Spreadsheet,
    portfolio_history: list[dict],
    spy_monthly_returns: dict,
    widget_rows: list[list],
):
    """One row per calendar month from the earliest DB candle to today."""
    from collections import defaultdict

    monthly: dict[str, list[dict]] = defaultdict(list)
    for entry in portfolio_history:
        monthly[entry["date"][:7]].append(entry)

    headers = [
        "Month", "Open Value", "Close Value",
        "Monthly P&L $", "Monthly P&L %",
        "SPY Return %", "Alpha %",
        "Cumulative P&L $", "Cumulative P&L %",
    ]

    data_rows: list[list] = []
    first_value: float | None = None

    for month in sorted(monthly.keys()):
        entries   = sorted(monthly[month], key=lambda x: x["date"])
        open_val  = entries[0]["value"]
        close_val = entries[-1]["value"]

        if first_value is None:
            first_value = open_val

        m_pnl     = round(close_val - open_val, 2)
        m_pnl_pct = round(m_pnl / open_val * 100, 2) if open_val else 0
        cum_pnl   = round(close_val - first_value, 2)
        cum_pct   = round(cum_pnl / first_value * 100, 2) if first_value else 0

        spy_ret   = spy_monthly_returns.get(month)
        spy_pct   = round(spy_ret * 100, 2) if spy_ret is not None else ""
        alpha     = round(m_pnl_pct - spy_pct, 2) if spy_pct != "" else ""

        data_rows.append([
            month,
            round(open_val, 2), round(close_val, 2),
            m_pnl, m_pnl_pct,
            spy_pct, alpha,
            cum_pnl, cum_pct,
        ])

    try:
        from database import get_closed_positions
        closed = get_closed_positions()
        if closed:
            earliest = min(cp.get("close_date") or "" for cp in closed)
            n_closed = len(closed)
            disclaimer = (
                f"Note: Values include realized P&L from {n_closed} closed position(s) "
                f"tracked since {earliest}. Positions closed before that date are not reflected. "
                "Overnight fees and spread costs are not included."
            )
        else:
            disclaimer = (
                "Note: No closed position records found. "
                "Run 'python main.py import-statement <csv>' to import historical trade data, "
                "or run 'python main.py sync-positions' to start tracking future closures."
            )
    except Exception:
        disclaimer = ""

    disclaimer_row = [[disclaimer]] if disclaimer else []
    all_rows = widget_rows + disclaimer_row + [headers] + data_rows
    _write_tab(spreadsheet, TAB_MONTHLY_PERF, all_rows)


# ── Tab: Daily Performance (YTD) ─────────────────────────────────────────────

def _export_daily_performance(
    spreadsheet: gspread.Spreadsheet,
    portfolio_history: list[dict],
    widget_rows: list[list],
):
    """One row per trading day, YTD (Jan 1 of current year to today). Newest row first."""
    year_start = f"{datetime.now(timezone.utc).year}-01-01"
    ytd = sorted(
        [h for h in portfolio_history if h["date"] >= year_start],
        key=lambda x: x["date"],
    )
    if not ytd:
        _write_tab(spreadsheet, TAB_DAILY_PERF, widget_rows + [["No YTD data available"]])
        return

    headers = ["Date", "Portfolio Value", "Day P&L $", "Day P&L %", "YTD P&L $", "YTD P&L %"]
    first_val = ytd[0]["value"]
    data_rows: list[list] = []

    for i, entry in enumerate(ytd):
        prev_val    = ytd[i - 1]["value"] if i > 0 else entry["value"]
        day_pnl     = round(entry["value"] - prev_val, 2) if i > 0 else ""
        day_pnl_pct = (round(day_pnl / prev_val * 100, 2) if prev_val and i > 0 else "")
        ytd_pnl     = round(entry["value"] - first_val, 2)
        ytd_pct     = round(ytd_pnl / first_val * 100, 2) if first_val else 0
        data_rows.append([
            entry["date"], round(entry["value"], 2),
            day_pnl, day_pnl_pct, ytd_pnl, ytd_pct,
        ])

    all_rows = widget_rows + [headers] + list(reversed(data_rows))
    _write_tab(spreadsheet, TAB_DAILY_PERF, all_rows)


# ── Tab: Daily P&L ───────────────────────────────────────────────────────────

def _export_daily_pnl(spreadsheet: gspread.Spreadsheet, positions: dict, portfolio: dict):
    """
    Append-only daily P&L log. Each export run writes one TOTAL row and one row per
    active ticker for today. Re-running on the same day replaces that day's rows.
    Newest data sits at the top (below headers).
    """
    headers = [
        "Date", "Ticker", "Direction", "Units", "Avg Entry Price", "Current Price",
        "Cost Basis", "Current Value", "Day P&L $", "Day P&L %",
        "Unrealized P&L $", "Unrealized P&L %",
    ]
    today = datetime.now(_BERLIN).strftime("%Y-%m-%d")

    ws = spreadsheet.worksheet(TAB_DAILY_PNL)
    existing = ws.get_all_values()

    # Preserve historical rows (not today), discarding any from today (idempotent re-runs)
    if existing and existing[0] == headers:
        history = [r for r in existing[1:] if r and r[0] != today]
    else:
        history = []

    ticker_rows: list[list] = []
    total_cost = total_value = total_unrealized = 0.0
    total_day_pnl = 0.0
    total_day_prev_value = 0.0

    for ticker, legs in positions.items():
        if not legs:
            continue
        p = portfolio.get(ticker, {})
        current_price = float(p.get("latest_close") or 0)
        prev_close    = float(p.get("prev_close")   or 0)
        if not current_price:
            continue

        agg = _aggregate_pnl(legs, current_price)
        if not agg:
            continue

        units      = agg["total_units"]
        cost       = agg["total_cost"]
        value      = agg["total_value"]
        unrealized = agg["total_pnl"]
        pnl_pct    = agg["pnl_pct"]

        if prev_close and units:
            prev_value  = round(units * prev_close, 2)
            day_pnl     = round(value - prev_value, 2)
            day_pnl_pct = round(day_pnl / prev_value * 100, 2) if prev_value else ""
            total_day_pnl        += day_pnl
            total_day_prev_value += prev_value
        else:
            day_pnl = day_pnl_pct = ""

        total_cost       += cost
        total_value      += value
        total_unrealized += unrealized

        ticker_rows.append([
            today, ticker, agg["direction"],
            round(units, 4), agg["avg_entry"], current_price,
            cost, value,
            day_pnl, day_pnl_pct,
            unrealized, pnl_pct,
        ])

    total_pnl_pct     = round(total_unrealized / total_cost * 100, 2) if total_cost else ""
    total_day_pnl_val = round(total_day_pnl, 2)    if total_day_prev_value else ""
    total_day_pct     = round(total_day_pnl / total_day_prev_value * 100, 2) if total_day_prev_value else ""

    total_row: list = [
        today, "TOTAL", "", "", "", "",
        round(total_cost,       2),
        round(total_value,      2),
        total_day_pnl_val, total_day_pct,
        round(total_unrealized, 2), total_pnl_pct,
    ]

    all_rows = [headers, total_row] + ticker_rows + history
    ws.clear()
    ws.update("A1", all_rows, value_input_option="USER_ENTERED")
    log.info("Daily P&L: %d today rows + %d historical rows", len(ticker_rows) + 1, len(history))


# ── Tab: Closed Trades ───────────────────────────────────────────────────────

def _export_closed_trades(spreadsheet: gspread.Spreadsheet):
    """
    Write all locally-recorded closed positions to the Closed Trades tab.
    Sources: 'auto' (detected via sync) or 'import' (eToro statement CSV).
    Sorted newest-first.
    """
    from database import get_closed_positions
    closed = get_closed_positions()

    headers = [
        "Ticker", "Direction", "Units", "Entry Price", "Exit Price",
        "Open Date", "Close Date", "Realized P&L $", "P&L %", "Source",
    ]

    data_rows: list[list] = []
    for cp in reversed(closed):  # newest first
        units      = cp.get("units") or 0
        open_price = cp.get("open_price") or 0
        close_price = cp.get("close_price") or 0
        realized   = cp.get("realized_pnl") or 0
        cost       = units * open_price if units and open_price else 0
        pnl_pct    = round(realized / cost * 100, 2) if cost else ""
        data_rows.append([
            cp.get("ticker", ""),
            cp.get("direction", "BUY"),
            units,
            open_price or "",
            close_price or "",
            cp.get("open_date", ""),
            cp.get("close_date", ""),
            round(realized, 2),
            pnl_pct,
            cp.get("source", ""),
        ])

    if not data_rows:
        rows = [
            ["No closed positions recorded yet."],
            [""],
            ["To populate this tab:"],
            ["  1. Run 'python main.py sync-positions' daily — closures are auto-detected."],
            ["  2. Or run 'python main.py import-statement <csv>' with your eToro account statement."],
        ]
    else:
        rows = [headers] + data_rows

    _write_tab(spreadsheet, TAB_CLOSED, rows)


# ── Tab: Metadata ─────────────────────────────────────────────────────────────

def _export_metadata(spreadsheet: gspread.Spreadsheet, trigger: str):
    now = datetime.now(_BERLIN).strftime("%Y-%m-%d %H:%M:%S CET")
    rows = [
        ["Key", "Value"],
        ["Last Updated", now],
        ["Trigger",      trigger],
        ["Tickers",      ", ".join(WATCHLIST_TICKERS)],
        ["Sheet ID",     GOOGLE_SHEET_ID],
    ]
    _write_tab(spreadsheet, TAB_META, rows)


# ── Public entry points ───────────────────────────────────────────────────────

def _fetch_spy_data() -> tuple[pd.DataFrame | None, dict[str, float], float | None]:
    """
    Fetch SPY daily + monthly data. Returns:
      - spy_daily_df: DataFrame with 'close' column (for regime detection)
      - spy_monthly_returns: {YYYY-MM: return_decimal}
      - spy_ytd_return: YTD % return as float (or None)
    """
    spy_daily_df: pd.DataFrame | None = None
    spy_monthly_returns: dict[str, float] = {}
    spy_ytd_return: float | None = None

    try:
        # Daily data — 5 years needed for 200-day EMA
        raw_d = yf.download("SPY", period="5y", interval="1d", progress=False, auto_adjust=True)
        if not raw_d.empty:
            if isinstance(raw_d.columns, pd.MultiIndex):
                spy_close_d = raw_d["Close"].iloc[:, 0].dropna()
            else:
                spy_close_d = raw_d["Close"].dropna()
            spy_daily_df = pd.DataFrame({"close": spy_close_d.values}, index=spy_close_d.index)

            year_start = f"{datetime.now(timezone.utc).year}-01-01"
            ytd_s = spy_close_d[spy_close_d.index >= pd.Timestamp(year_start)]
            if len(ytd_s) >= 2:
                spy_ytd_return = round((float(ytd_s.iloc[-1]) / float(ytd_s.iloc[0]) - 1) * 100, 2)

        # Monthly data — for SPY Return % / Alpha columns in Monthly Performance tab
        raw_m = yf.download("SPY", period="5y", interval="1mo", progress=False, auto_adjust=True)
        if not raw_m.empty:
            if isinstance(raw_m.columns, pd.MultiIndex):
                spy_close_m = raw_m["Close"].iloc[:, 0].dropna()
            else:
                spy_close_m = raw_m["Close"].dropna()
            spy_mo_ret = spy_close_m.pct_change().dropna()
            for dt_idx, ret_val in spy_mo_ret.items():
                spy_monthly_returns[str(dt_idx)[:7]] = float(ret_val)

    except Exception as e:
        log.warning("SPY data fetch failed: %s", e)

    return spy_daily_df, spy_monthly_returns, spy_ytd_return


def run_export(trigger: str = "scheduled"):
    """Export all trading data to Google Sheets. Called by the scheduler or CLI."""
    from pipeline import backfill, refresh, sync_positions

    log.info("Starting Google Sheets export (trigger=%s)", trigger)

    # Pull the latest open positions from eToro before reading positions.json
    # so the dashboard always reflects current broker state (no manual sync needed).
    try:
        sync_positions()
    except Exception as e:
        log.warning("sync_positions failed (using stale positions.json): %s", e)

    client      = _get_client()
    spreadsheet = _open_spreadsheet(client)

    # Discover effective ticker list from positions + config watchlist
    positions         = _load_positions()
    effective_tickers = _get_effective_tickers(positions)

    # Backfill tickers that have no price data yet (e.g. new positions)
    have_data   = {r["ticker"] for r in get_portfolio_summary()}
    new_tickers = [t for t in effective_tickers if t not in have_data]
    if new_tickers:
        log.info("Backfilling new position tickers: %s", new_tickers)
        backfill(new_tickers)

    # Refresh stale tickers (skips fresh ones automatically)
    refresh(effective_tickers)

    _ensure_tabs(spreadsheet, effective_tickers)

    portfolio = {r["ticker"]: r for r in get_portfolio_summary()}
    log.info("Active positions: %s", {t: len(v) for t, v in positions.items()})

    # ── Live prices from eToro (overrides stale DB closes for P&L accuracy) ──
    log.info("Fetching live prices from eToro...")
    live_prices = _fetch_live_prices(effective_tickers)
    for ticker, price in live_prices.items():
        if ticker in portfolio:
            portfolio[ticker] = dict(portfolio[ticker])   # don't mutate the original row
            portfolio[ticker]["latest_close"] = price
        else:
            portfolio[ticker] = {"latest_close": price}

    log.info("Fetching fundamentals from yfinance...")
    fundamentals = _fetch_fundamentals(effective_tickers)

    timestamp = datetime.now(_BERLIN).strftime("%Y-%m-%d %H:%M CET")

    # ── SPY data ──────────────────────────────────────────────────────────────
    log.info("Fetching SPY data for regime detection and alpha calculation...")
    spy_daily_df, spy_monthly_returns, spy_ytd_return = _fetch_spy_data()

    # ── Portfolio history ─────────────────────────────────────────────────────
    from_date = "2021-01-01"
    to_date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info("Reconstructing portfolio history %s → %s...", from_date, to_date)
    portfolio_history = _build_portfolio_history(positions, from_date, to_date)

    # Patch today's equity with live prices so Daily Performance is accurate.
    # Uses P&L formula: INITIAL_CASH + unrealised P&L (live) + cumulative realised P&L.
    if live_prices:
        today_str = datetime.now(_BERLIN).strftime("%Y-%m-%d")
        try:
            from database import get_closed_positions
            _closed_all = get_closed_positions(from_date="2000-01-01", to_date=today_str)
            _realized_total = sum(float(cp.get("realized_pnl") or 0) for cp in (_closed_all or []))
        except Exception:
            _realized_total = 0.0

        _live_unrealized = 0.0
        for ticker, legs in positions.items():
            price = live_prices.get(ticker) or float(portfolio.get(ticker, {}).get("latest_close") or 0)
            if not price:
                continue
            for leg in legs:
                units = float(leg.get("units", 0))
                entry = float(leg.get("open_price", 0))
                if units <= 0 or entry <= 0:
                    continue
                direction = leg.get("direction", "BUY").upper()
                pnl = (price - entry) if direction == "BUY" else (entry - price)
                _live_unrealized += units * pnl

        live_equity = round(float(INITIAL_CASH) + _live_unrealized + _realized_total, 2)
        if portfolio_history and portfolio_history[-1]["date"] == today_str:
            portfolio_history[-1]["value"] = live_equity
        else:
            portfolio_history.append({"date": today_str, "value": live_equity})

    portfolio_returns: list[float] = []
    for i in range(1, len(portfolio_history)):
        prev = portfolio_history[i - 1]["value"]
        curr = portfolio_history[i]["value"]
        if prev > 0:
            portfolio_returns.append((curr - prev) / prev)

    # ── Regime detection ──────────────────────────────────────────────────────
    regime_info = _detect_regime(spy_daily_df, portfolio_returns)
    regime      = regime_info["regime"]
    log.info("Market regime: %s (SPY>200MA=%s, vol=%.1f%%)",
             regime, regime_info["spy_above_200ma"], regime_info["realised_vol"] * 100)

    # ── Per-ticker IC weights ─────────────────────────────────────────────────
    ticker_weights: dict[str, dict] = {}
    for ticker in effective_tickers:
        df = _build_df(ticker, days=250)
        if df is not None and len(df) >= 40:
            raw_w = _compute_signal_weights(df)
            ticker_weights[ticker] = _apply_regime_multipliers(raw_w, regime) if raw_w else {}
        else:
            ticker_weights[ticker] = {}

    # ── Risk metrics ──────────────────────────────────────────────────────────
    risk = _compute_risk_metrics(positions, portfolio, fundamentals, portfolio_history, portfolio_returns)
    log.info(
        "Risk: σ=%.1f%% VaR95=%.2f%% Sortino=%.2f Beta+HHI=%.1f MaxDD=%.1f%%",
        (risk.get("volatility_annual") or 0) * 100,
        (risk.get("var_95_pct") or 0) * 100,
        risk.get("sortino_ratio") or 0,
        risk.get("composite_score") or 0,
        risk.get("max_drawdown_pct") or 0,
    )

    # ── YTD portfolio return ──────────────────────────────────────────────────
    year_start            = f"{datetime.now(timezone.utc).year}-01-01"
    ytd_hist              = [h for h in portfolio_history if h["date"] >= year_start]
    portfolio_ytd_return: float | None = None
    if len(ytd_hist) >= 2:
        portfolio_ytd_return = round(
            (ytd_hist[-1]["value"] / ytd_hist[0]["value"] - 1) * 100, 2
        )

    # ── Widget rows (shared across Overview, Monthly Perf, Daily Perf) ────────
    widget_rows = _build_widget_rows(
        risk, positions, portfolio, fundamentals,
        spy_ytd_return, portfolio_ytd_return, timestamp,
    )

    # ── Decide which tabs to refresh ─────────────────────────────────────────
    # Full export (open / close / manual / daily): all tabs.
    # Hourly (intraday): only live-sensitive tabs — skip heavy static ones.
    FULL_TRIGGERS = {"market_open", "market_close", "daily_refresh", "manual"}
    full = trigger in FULL_TRIGGERS
    log.info("Export mode: %s (%s)", "FULL" if full else "LIVE-ONLY", trigger)

    # ── Always: live P&L widget + Overview + Live Overview + Daily Perf + per-ticker ──
    _export_overview(
        spreadsheet, positions, fundamentals, effective_tickers,
        widget_rows, ticker_weights, regime,
    )
    _export_live_overview(
        spreadsheet, positions, fundamentals, effective_tickers,
        portfolio, widget_rows, ticker_weights, regime,
    )
    _export_daily_performance(spreadsheet, portfolio_history, widget_rows)

    for ticker in effective_tickers:
        _export_stock_tab(
            spreadsheet, ticker, portfolio, positions, fundamentals,
            weights=ticker_weights.get(ticker),
            regime=regime,
        )

    # ── Full export only: heavy / append-only / historical tabs ──────────────
    if full:
        _write_positions_tab(spreadsheet, positions, effective_tickers)
        _export_logbook(spreadsheet, fundamentals, effective_tickers)
        _export_daily_pnl(spreadsheet, positions, portfolio)
        _export_monthly_performance(spreadsheet, portfolio_history, spy_monthly_returns, widget_rows)
        _export_closed_trades(spreadsheet)

    _export_metadata(spreadsheet, trigger=trigger)

    log.info(
        "Google Sheets export complete → https://docs.google.com/spreadsheets/d/%s",
        GOOGLE_SHEET_ID,
    )


def setup_sheets():
    """
    Interactive setup: test credentials, create the spreadsheet, and print
    the share URL. Run once after placing your service account JSON key.
    """
    print("\n── Google Sheets Setup ────────────────────────────────────────────")

    if not os.path.exists(GOOGLE_SHEETS_CREDENTIALS_PATH):
        print(f"\n  ERROR: Credentials file not found at:\n  {GOOGLE_SHEETS_CREDENTIALS_PATH}")
        print("""
  Steps to create a Google service account:

  1. Go to https://console.cloud.google.com/
  2. Create a new project (or select an existing one)
  3. Enable these two APIs:
       • Google Sheets API
       • Google Drive API
  4. Go to IAM & Admin → Service Accounts → Create Service Account
  5. Name it (e.g. "trading-sheets-bot"), click Create
  6. Skip optional role steps, click Done
  7. Click the service account → Keys tab → Add Key → JSON
  8. Save the downloaded JSON as:
       {path}
  9. Re-run: python main.py setup-sheets
""".format(path=GOOGLE_SHEETS_CREDENTIALS_PATH))
        return

    print(f"  ✓ Credentials file found: {GOOGLE_SHEETS_CREDENTIALS_PATH}")

    try:
        client = _get_client()
        print("  ✓ Google API authentication successful")
    except Exception as e:
        print(f"  ✗ Authentication failed: {e}")
        return

    try:
        if GOOGLE_SHEET_ID:
            spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
            print(f"  ✓ Opened existing spreadsheet: {spreadsheet.title}")
        else:
            spreadsheet = client.create(GOOGLE_SHEET_NAME)
            print(f"  ✓ Created new spreadsheet: {spreadsheet.title}")
            print(f"\n  *** Copy this Sheet ID into config.py → GOOGLE_SHEET_ID ***")
            print(f"  Sheet ID: {spreadsheet.id}")
    except Exception as e:
        print(f"  ✗ Could not open/create spreadsheet: {e}")
        return

    try:
        spreadsheet.share(None, perm_type="anyone", role="reader")
        print("  ✓ Sheet is now viewable by anyone with the link")
    except Exception:
        pass

    _ensure_tabs(spreadsheet, WATCHLIST_TICKERS)
    all_tabs = STATIC_TABS + WATCHLIST_TICKERS
    print(f"  ✓ All tabs ready: {', '.join(all_tabs)}")

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet.id}"
    print(f"\n  Sheet URL: {url}")
    print("""
  Next steps:
  1. Run a test export: python main.py export
  2. Start the scheduler: python main.py scheduler
     The sheet will auto-update at 09:30 and 16:00 New York time every weekday.
──────────────────────────────────────────────────────────────────────""")
