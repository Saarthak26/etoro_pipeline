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
from datetime import datetime, timezone

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
)

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

TAB_POSITIONS = "Positions"       # ← user edits this tab; it's the source of truth
TAB_OVERVIEW  = "Overview"
TAB_LOGBOOK   = "Log Book"
TAB_META      = "Metadata"
STATIC_TABS   = [TAB_POSITIONS, TAB_OVERVIEW, TAB_LOGBOOK, TAB_META]

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


def _write_positions_tab(spreadsheet: gspread.Spreadsheet, positions: dict[str, list[dict]]):
    """Write all positions to the Positions tab so the user can edit them in the sheet."""
    rows = [POSITIONS_HEADERS]
    for ticker in WATCHLIST_TICKERS:
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


def _ensure_tabs(spreadsheet: gspread.Spreadsheet):
    """Create any missing tabs; delete stale ones that no longer match the watchlist."""
    existing   = {ws.title: ws for ws in spreadsheet.worksheets()}
    wanted     = set(STATIC_TABS + WATCHLIST_TICKERS)

    # Remove tabs that are no longer needed (old layout tabs, removed tickers)
    stale = [t for t in existing if t not in wanted]
    for title in stale:
        spreadsheet.del_worksheet(existing[title])
        log.info("Deleted stale tab: %s", title)

    # Create missing tabs
    for tab in STATIC_TABS + WATCHLIST_TICKERS:
        if tab not in existing:
            spreadsheet.add_worksheet(title=tab, rows=500, cols=30)
            log.info("Created tab: %s", tab)


def _write_tab(spreadsheet: gspread.Spreadsheet, title: str, rows: list[list]):
    ws = spreadsheet.worksheet(title)
    ws.clear()
    if rows:
        ws.update("A1", rows, value_input_option="USER_ENTERED")
    log.info("Written %d rows to '%s'", len(rows), title)


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


# ── Tab: Overview ─────────────────────────────────────────────────────────────

def _export_overview(spreadsheet: gspread.Spreadsheet, positions: dict):
    headers = [
        "Ticker", "Date", "Close", "Open", "High", "Low", "Volume",
        "Day Change %", "Trend",
        "RSI", "MACD", "MACD Signal", "MACD Hist",
        "EMA20", "EMA50", "EMA200",
        "BB Upper", "BB Middle", "BB Lower",
        "ATR", "Volume Ratio", "Fisher", "Fisher Signal",
        "Signal Reason",
        # Aggregate position columns (across all legs)
        "Direction", "Total Units", "Avg Entry", "# Legs",
        "Total Cost", "Current Value", "Unrealized P&L", "P&L %",
    ]
    portfolio = {r["ticker"]: r for r in get_portfolio_summary()}
    rows = [headers]

    for ticker in WATCHLIST_TICKERS:
        p     = portfolio.get(ticker, {})
        df    = _build_df(ticker)
        ind   = _compute_indicators(df) if df is not None else {}
        legs  = positions.get(ticker, [])
        close = p.get("latest_close") or 0
        agg   = _aggregate_pnl(legs, float(close)) if legs and close else {}

        rows.append([
            ticker,
            p.get("latest_date", ""),
            p.get("latest_close", ""),
            p.get("latest_open", ""),
            p.get("latest_high", ""),
            p.get("latest_low", ""),
            p.get("latest_volume", ""),
            p.get("day_change_pct", ""),
            ind.get("trend", ""),
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
            _signal_reason(ind, ticker) if ind else "",
            agg.get("direction", ""),
            agg.get("total_units", ""),
            agg.get("avg_entry", ""),
            len(legs) if legs else "",
            agg.get("total_cost", ""),
            agg.get("total_value", ""),
            agg.get("total_pnl", ""),
            agg.get("pnl_pct", ""),
        ])

    _write_tab(spreadsheet, TAB_OVERVIEW, rows)


# ── Tab: Per-stock ────────────────────────────────────────────────────────────

def _export_stock_tab(spreadsheet: gspread.Spreadsheet, ticker: str, portfolio: dict, positions: dict):
    p       = portfolio.get(ticker, {})
    df      = _build_df(ticker, days=250)
    ind     = _compute_indicators(df) if df is not None else {}
    legs    = positions.get(ticker, [])
    close   = p.get("latest_close") or 0
    agg     = _aggregate_pnl(legs, float(close)) if legs and close else {}
    day_chg = p.get("day_change_pct", "")

    rows = []

    # ── Section 0: Company ────────────────────────────────────────────────────
    rows.append(["COMPANY", ""])
    rows.append(["About", COMPANY_INFO.get(ticker, ticker)])
    rows.append(["", ""])

    # ── Section 1: Snapshot ───────────────────────────────────────────────────
    rows.append(["SNAPSHOT", ""])
    rows.append(["Ticker",       ticker])
    rows.append(["Date",         p.get("latest_date", "")])
    rows.append(["Close",        close])
    rows.append(["Open",         p.get("latest_open", "")])
    rows.append(["High",         p.get("latest_high", "")])
    rows.append(["Low",          p.get("latest_low", "")])
    rows.append(["Volume",       p.get("latest_volume", "")])
    rows.append(["Day Change %", day_chg])
    rows.append(["Prev Close",   p.get("prev_close", "")])
    rows.append(["Trend",        ind.get("trend", "")])
    rows.append(["Signal",       _signal_reason(ind, ticker) if ind else ""])
    rows.append(["", ""])

    # ── Section 2: Market Sentiment ───────────────────────────────────────────
    rows.append(["MARKET SENTIMENT & CONDITION", ""])
    rows.append(["Analysis", _market_sentiment(ind, float(close), day_chg) if ind else "Insufficient data."])
    rows.append(["", ""])

    # ── Section 2: Open Positions ─────────────────────────────────────────────
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

    # ── Section 3: Indicators ─────────────────────────────────────────────────
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

    # ── Section 4: OHLCV history ──────────────────────────────────────────────
    rows.append(["OHLCV HISTORY (90 days)", "", "", "", "", ""])
    rows.append(["Date", "Open", "High", "Low", "Close", "Volume", "Change %"])

    candles = get_candles(ticker, days=90)
    for i, c in enumerate(candles):
        prev_close = candles[i - 1]["close"] if i > 0 else None
        chg = round((c["close"] - prev_close) / prev_close * 100, 2) if prev_close else ""
        rows.append([c["date"], c["open"], c["high"], c["low"], c["close"], c["volume"], chg])

    _write_tab(spreadsheet, ticker, rows)


# ── yfinance fundamentals ─────────────────────────────────────────────────────

def _fetch_fundamentals(tickers: list[str]) -> dict[str, dict]:
    """Fetch market cap, sector, 52W high/low for all tickers in one batch call."""
    result = {}
    try:
        data = yf.Tickers(" ".join(tickers))
        for ticker in tickers:
            try:
                info = data.tickers[ticker].fast_info
                full_info = data.tickers[ticker].info
                result[ticker] = {
                    "market_cap":   _fmt_market_cap(getattr(info, "market_cap", None)),
                    "sector":       full_info.get("sector", ""),
                    "week52_high":  round(float(getattr(info, "year_high", 0) or 0), 2),
                    "week52_low":   round(float(getattr(info, "year_low",  0) or 0), 2),
                    "shares_out":   getattr(info, "shares", None),
                }
            except Exception:
                result[ticker] = {"market_cap": "", "sector": "", "week52_high": "", "week52_low": ""}
    except Exception as e:
        log.warning("yfinance batch fetch failed: %s", e)
        for t in tickers:
            result[t] = {"market_cap": "", "sector": "", "week52_high": "", "week52_low": ""}
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

def _export_logbook(spreadsheet: gspread.Spreadsheet, fundamentals: dict):
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

    for ticker in WATCHLIST_TICKERS:
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


# ── Tab: Metadata ─────────────────────────────────────────────────────────────

def _export_metadata(spreadsheet: gspread.Spreadsheet, trigger: str):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = [
        ["Key", "Value"],
        ["Last Updated", now],
        ["Trigger",      trigger],
        ["Tickers",      ", ".join(WATCHLIST_TICKERS)],
        ["Sheet ID",     GOOGLE_SHEET_ID],
    ]
    _write_tab(spreadsheet, TAB_META, rows)


# ── Public entry points ───────────────────────────────────────────────────────

def run_export(trigger: str = "scheduled"):
    """Export all trading data to Google Sheets. Called by the scheduler or CLI."""
    log.info("Starting Google Sheets export (trigger=%s)", trigger)
    client      = _get_client()
    spreadsheet = _open_spreadsheet(client)
    _ensure_tabs(spreadsheet)

    # ── 1. Sync positions: sheet → positions.json (sheet is source of truth) ──
    _sync_positions_from_sheet(spreadsheet)

    portfolio = {r["ticker"]: r for r in get_portfolio_summary()}
    positions = _load_positions()
    log.info("Active positions: %s", {t: len(v) for t, v in positions.items()})

    log.info("Fetching fundamentals from yfinance (market cap, sector, 52W H/L)...")
    fundamentals = _fetch_fundamentals(WATCHLIST_TICKERS)

    # ── 2. Write all computed tabs ─────────────────────────────────────────────
    _write_positions_tab(spreadsheet, positions)   # keep Positions tab tidy
    _export_overview(spreadsheet, positions)
    _export_logbook(spreadsheet, fundamentals)
    for ticker in WATCHLIST_TICKERS:
        _export_stock_tab(spreadsheet, ticker, portfolio, positions)
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

    _ensure_tabs(spreadsheet)
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
