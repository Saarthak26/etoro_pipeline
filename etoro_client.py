from __future__ import annotations

"""
etoro_client.py — Authenticated HTTP client for the eToro Public API.

Handles:
  - Auth headers on every request (x-api-key, x-user-key, x-request-id)
  - Exponential backoff on 429 Too Many Requests and 5xx errors
  - Rate limiting: pauses REQUEST_DELAY_SECONDS between calls
  - Clean error messages so failures are obvious and actionable
"""

import time
import uuid
import logging
import requests

from config import (
    BASE_URL,
    ETORO_API_KEY,
    ETORO_USER_KEY,
    REQUEST_DELAY_SECONDS,
    MAX_RETRIES,
    BACKOFF_FACTOR,
)

log = logging.getLogger(__name__)


class EToroAPIError(Exception):
    """Raised when the eToro API returns an unrecoverable error."""
    pass


class EToroClient:
    """
    Thin wrapper around requests.Session that adds eToro authentication
    and resilient retry behaviour.

    Usage:
        client = EToroClient()
        data   = client.get("/market-data/search", params={"internalSymbolFull": "NVDA"})
    """

    def __init__(self):
        self.session  = requests.Session()
        self.base_url = BASE_URL
        self._last_request_time = 0.0
        self._instruments_cache: list | None = None

    # ── Public methods ────────────────────────────────────────────────────────

    def get(self, path: str, params: dict = None) -> dict:
        """
        Perform an authenticated GET request with retry logic.

        Args:
            path:   API path relative to BASE_URL (e.g. "/market-data/search")
            params: Optional query parameters dict

        Returns:
            Parsed JSON response as a dict or list.

        Raises:
            EToroAPIError: On auth failures (401/403) or repeated failures.
        """
        url = f"{self.base_url}{path}"
        return self._request_with_retry("GET", url, params=params)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_headers(self) -> dict:
        """Build the three required eToro auth headers plus a fresh request ID."""
        return {
            "x-api-key":    ETORO_API_KEY,
            "x-user-key":   ETORO_USER_KEY,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def _throttle(self):
        """Ensure at least REQUEST_DELAY_SECONDS between consecutive requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < REQUEST_DELAY_SECONDS:
            time.sleep(REQUEST_DELAY_SECONDS - elapsed)
        self._last_request_time = time.time()

    def _request_with_retry(self, method: str, url: str, **kwargs) -> dict:
        """Core request loop with exponential backoff."""
        attempt      = 0
        wait_seconds = 2

        while attempt < MAX_RETRIES:
            self._throttle()
            headers = self._build_headers()

            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=15,
                    **kwargs,
                )

                # ── Auth errors — no point retrying ──────────────────────────
                if response.status_code in (401, 403):
                    raise EToroAPIError(
                        f"Auth failed ({response.status_code}). "
                        "Check your ETORO_API_KEY and ETORO_USER_KEY in config.py.\n"
                        f"Response: {response.text}"
                    )

                # ── Rate limited — back off and retry ─────────────────────────
                if response.status_code == 429:
                    log.warning(f"Rate limited. Waiting {wait_seconds}s before retry {attempt + 1}/{MAX_RETRIES}.")
                    time.sleep(wait_seconds)
                    wait_seconds *= BACKOFF_FACTOR
                    attempt += 1
                    continue

                # ── Server errors — back off and retry ────────────────────────
                if response.status_code >= 500:
                    log.warning(f"Server error {response.status_code}. Retry {attempt + 1}/{MAX_RETRIES}.")
                    time.sleep(wait_seconds)
                    wait_seconds *= BACKOFF_FACTOR
                    attempt += 1
                    continue

                # ── Success ───────────────────────────────────────────────────
                response.raise_for_status()
                return response.json()

            except requests.exceptions.Timeout:
                log.warning(f"Request timed out. Retry {attempt + 1}/{MAX_RETRIES}.")
                time.sleep(wait_seconds)
                wait_seconds *= BACKOFF_FACTOR
                attempt += 1

            except requests.exceptions.ConnectionError as exc:
                log.warning(f"Connection error: {exc}. Retry {attempt + 1}/{MAX_RETRIES}.")
                time.sleep(wait_seconds)
                wait_seconds *= BACKOFF_FACTOR
                attempt += 1

        raise EToroAPIError(
            f"All {MAX_RETRIES} retries failed for {method} {url}. "
            "Check your internet connection and eToro API status."
        )

    # ── Market data helpers (typed convenience methods) ───────────────────────

    def search_instrument(self, ticker: str) -> dict | None:
        """
        Resolve a ticker symbol to its eToro instrument metadata.

        Args:
            ticker: e.g. "NVDA", "AMD"

        Returns:
            Best matching instrument dict, or None if not found.
        """
        log.info(f"Resolving ticker: {ticker}")
        if self._instruments_cache is None:
            result = self.get("/market-data/instruments")
            self._instruments_cache = result.get("instrumentDisplayDatas", [])
            log.info(f"Loaded {len(self._instruments_cache)} instruments from eToro")

        ticker_upper = ticker.upper()
        for inst in self._instruments_cache:
            if inst.get("symbolFull", "").upper() == ticker_upper:
                return inst

        log.warning(f"No instrument found for ticker: {ticker}")
        return None

    def get_candles(
        self,
        instrument_id: int,
        interval: str,
        candles_count: int,
        direction: str = "desc",
    ) -> list[dict]:
        """
        Fetch OHLCV candles for an instrument.

        Args:
            instrument_id: eToro numeric instrument ID
            interval:      e.g. "OneDay", "OneHour"
            candles_count: Number of candles (max 1000)
            direction:     "desc" = newest first, "asc" = oldest first

        Returns:
            List of candle dicts with keys: fromDate, open, high, low, close, volume
        """
        path = (
            f"/market-data/instruments/{instrument_id}"
            f"/history/candles/{direction}/{interval}/{candles_count}"
        )
        log.info(f"Fetching {candles_count} {interval} candles for instrument {instrument_id}")
        response = self.get(path)

        # Response structure: { interval: "...", candles: [ { instrumentId, candles: [...] } ] }
        candle_groups = response.get("candles", [])
        if not candle_groups:
            return []

        return candle_groups[0].get("candles", [])

    def get_portfolio(self) -> list[dict]:
        """
        Fetch all open positions from the authenticated portfolio endpoint.

        Returns:
            List of position dicts from clientPortfolio.positions.
        """
        log.info("Fetching live portfolio positions...")
        result = self.get("/trading/info/portfolio")
        return result.get("clientPortfolio", {}).get("positions", [])

    def get_instrument_by_id(self, instrument_id: int) -> dict | None:
        """
        Look up an instrument by its numeric ID using the cached instruments list.

        Useful for resolving IDs returned by the portfolio endpoint that are not
        yet in the local SQLite DB.
        """
        if self._instruments_cache is None:
            result = self.get("/market-data/instruments")
            self._instruments_cache = result.get("instrumentDisplayDatas", [])

        for inst in self._instruments_cache:
            if inst.get("instrumentID") == instrument_id or inst.get("internalInstrumentId") == instrument_id:
                return inst
        return None

    def get_current_rates(self, instrument_ids: list[int]) -> list[dict]:
        """
        Fetch live bid/ask/execution prices for a list of instruments.

        Args:
            instrument_ids: List of eToro numeric IDs

        Returns:
            List of rate dicts with current pricing info.
        """
        ids_param = ",".join(str(i) for i in instrument_ids)
        result    = self.get(
            "/market-data/instruments/rates",
            params={"instrumentIds": ids_param},
        )
        return result if isinstance(result, list) else result.get("rates", [])
