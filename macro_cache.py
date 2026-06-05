"""
macro_cache.py — Daily macro data fetch and local cache for each ticker.

Fetches from yfinance (no extra API key required):
  - Recent news headlines
  - Analyst 12-month price target
  - Sector / industry / market cap
  - 52-week high / low
  - Short ratio, institutional ownership %

Cache is a JSON file keyed by ticker. Data is refreshed once per calendar day.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import yfinance as yf

from config import MACRO_CACHE_PATH, WATCHLIST_TICKERS

log = logging.getLogger(__name__)

_STX_MAP = {"STX.US": "STX"}   # eToro uses STX.US; Yahoo knows it as STX


def _yahoo_ticker(ticker: str) -> str:
    return _STX_MAP.get(ticker, ticker)


def _load_cache() -> dict:
    if os.path.exists(MACRO_CACHE_PATH):
        try:
            with open(MACRO_CACHE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict) -> None:
    with open(MACRO_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _fetch_macro(ticker: str) -> dict:
    """Fetch macro data for a single ticker from yfinance."""
    yt = _yahoo_ticker(ticker)
    try:
        t    = yf.Ticker(yt)
        info = t.info or {}

        # ── News headlines ────────────────────────────────────────────────────
        news_raw = []
        try:
            news_raw = t.news or []
        except Exception:
            pass

        news = []
        for item in news_raw[:5]:
            ts = item.get("providerPublishTime") or item.get("publishedAt") or 0
            try:
                date_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                date_str = ""
            news.append({
                "date":      date_str,
                "title":     item.get("title", ""),
                "publisher": item.get("publisher", ""),
            })

        # ── Key fundamentals / macro stats ────────────────────────────────────
        mkt_cap = info.get("marketCap")
        if mkt_cap:
            if mkt_cap >= 1e12:
                mkt_cap_str = f"${mkt_cap/1e12:.2f}T"
            elif mkt_cap >= 1e9:
                mkt_cap_str = f"${mkt_cap/1e9:.1f}B"
            else:
                mkt_cap_str = f"${mkt_cap/1e6:.0f}M"
        else:
            mkt_cap_str = ""

        analyst_target = info.get("targetMeanPrice") or info.get("targetMedianPrice")

        return {
            "date":             _today(),
            "news":             news,
            "analyst_target":   round(float(analyst_target), 2) if analyst_target else None,
            "sector":           info.get("sector", ""),
            "industry":         info.get("industry", ""),
            "market_cap":       mkt_cap_str,
            "52w_high":         info.get("fiftyTwoWeekHigh"),
            "52w_low":          info.get("fiftyTwoWeekLow"),
            "short_ratio":      info.get("shortRatio"),
            "institutional_pct": round(info.get("heldPercentInstitutions", 0) * 100, 1)
                                  if info.get("heldPercentInstitutions") else None,
        }

    except Exception as exc:
        log.warning("Macro fetch failed for %s: %s", ticker, exc)
        return {"date": _today(), "news": [], "error": str(exc)}


def get_macro(ticker: str) -> dict:
    """Return today's cached macro dict for ticker, fetching fresh if needed."""
    cache  = _load_cache()
    today  = _today()
    cached = cache.get(ticker, {})

    if cached.get("date") == today:
        return cached

    log.info("Fetching macro data for %s...", ticker)
    data        = _fetch_macro(ticker)
    cache[ticker] = data
    _save_cache(cache)
    return data


def refresh_all_macro(tickers: list[str] | None = None) -> None:
    """Force-refresh macro for all tickers regardless of cache date."""
    tickers = tickers or WATCHLIST_TICKERS
    cache   = _load_cache()
    for ticker in tickers:
        log.info("Refreshing macro: %s", ticker)
        data           = _fetch_macro(ticker)
        cache[ticker]  = data
        _save_cache(cache)
        time.sleep(0.5)   # be polite to Yahoo
    log.info("Macro cache refreshed for %d tickers → %s", len(tickers), MACRO_CACHE_PATH)
