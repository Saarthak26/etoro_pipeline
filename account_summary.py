"""
account_summary.py — Read-only diagnostic for eToro broker-authoritative numbers.

Pulls the full /trading/info/portfolio response, dumps the raw JSON to
account_snapshot.json for inspection, and prints:
  • Account-level fields the broker reports (credit, totalProfit, etc.)
  • Per-position broker numbers (amount, units, openRate, leverage, profit)
    side-by-side with our currently-computed unrealised P&L.
  • Aggregate diffs: broker Σ amount / Σ profit vs sheet Σ units×open_price
    and our computed unrealised total.

Does not touch positions.json, the DB, or the sheet.

Realised P&L is NOT in the portfolio endpoint — eToro exposes lifetime
realised P&L only via the Account Statement (manual CSV). The diagnostic
prints whatever is in the closed_positions table for comparison and notes
the gap explicitly.
"""

from __future__ import annotations

import json
import logging
import os

from config import POSITIONS_PATH, DB_PATH
from etoro_client import EToroClient
from database import (
    initialise_db,
    get_ticker_by_instrument_id,
    get_latest_close,
    get_closed_positions,
)

log = logging.getLogger(__name__)

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "account_snapshot.json")


def _money(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _pct(x) -> str:
    try:
        return f"{float(x):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _sheet_unrealised(ticker: str, units: float, open_price: float, is_buy: bool) -> float | None:
    """Replicate the sheet's per-leg unrealised P&L using the latest daily close."""
    latest = get_latest_close(ticker)
    if not latest:
        return None
    close = float(latest["close"])
    return (close - open_price) * units if is_buy else (open_price - close) * units


def run_account_summary() -> None:
    initialise_db()
    client = EToroClient()
    snapshot = client.get_account_snapshot()

    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)
    log.info("Raw snapshot written to %s", SNAPSHOT_PATH)

    positions = snapshot.get("positions") or []
    account_keys = [k for k in snapshot.keys() if k != "positions"]

    print()
    print("═" * 78)
    print(" eToro Account Snapshot — broker-authoritative numbers")
    print("═" * 78)

    # ── Account-level (everything except positions) ──────────────────────────
    print("\nAccount-level fields:")
    if not account_keys:
        print("  (none — clientPortfolio wrapper returned only positions)")
    else:
        for k in sorted(account_keys):
            v = snapshot.get(k)
            if isinstance(v, (dict, list)):
                preview = json.dumps(v)[:120]
                print(f"  {k:<32} {preview}")
            else:
                print(f"  {k:<32} {v}")

    # ── Per-position broker vs sheet ─────────────────────────────────────────
    print("\nPer-position (broker side) vs sheet:")
    header = (
        f"{'Ticker':<8} {'Dir':<4} {'Units':>10} {'OpenRate':>10} "
        f"{'Lev':>5} {'Amount(B)':>12} {'Profit(B)':>12} "
        f"{'Cost(S)':>12} {'Unreal(S)':>12} {'ΔProfit':>10}"
    )
    print(header)
    print("─" * len(header))

    sum_broker_amount = 0.0
    sum_broker_profit = 0.0
    sum_sheet_cost    = 0.0
    sum_sheet_unreal  = 0.0

    for pos in positions:
        iid       = pos.get("instrumentID")
        ticker    = get_ticker_by_instrument_id(iid) or f"id={iid}"
        is_buy    = bool(pos.get("isBuy", True))
        direction = "BUY" if is_buy else "SELL"
        units     = float(pos.get("units") or 0)
        open_rate = float(pos.get("openRate") or 0)
        amount    = pos.get("amount")
        leverage  = pos.get("leverage")
        profit    = pos.get("profit")

        sheet_cost   = units * open_rate
        sheet_unreal = _sheet_unrealised(ticker, units, open_rate, is_buy)

        sum_broker_amount += float(amount or 0)
        sum_broker_profit += float(profit or 0)
        sum_sheet_cost    += sheet_cost
        if sheet_unreal is not None:
            sum_sheet_unreal += sheet_unreal

        diff_profit = (
            float(profit) - sheet_unreal
            if profit is not None and sheet_unreal is not None
            else None
        )

        print(
            f"{ticker:<8} {direction:<4} {units:>10.4f} {open_rate:>10.4f} "
            f"{str(leverage):>5} {_money(amount):>12} {_money(profit):>12} "
            f"{_money(sheet_cost):>12} "
            f"{_money(sheet_unreal) if sheet_unreal is not None else '—':>12} "
            f"{_money(diff_profit) if diff_profit is not None else '—':>10}"
        )

    # ── Aggregate diffs ──────────────────────────────────────────────────────
    print("─" * len(header))
    print(f"\nAggregates:")
    print(f"  Σ broker amount   (Net invested per broker)   {_money(sum_broker_amount)}")
    print(f"  Σ sheet exposure  (units × open_price)        {_money(sum_sheet_cost)}")
    print(f"  Δ Net invested                                {_money(sum_sheet_cost - sum_broker_amount)}  "
          f"({_pct((sum_sheet_cost - sum_broker_amount) / sum_broker_amount * 100) if sum_broker_amount else '—'})")
    print()
    print(f"  Σ broker profit   (Unrealised P&L per broker) {_money(sum_broker_profit)}")
    print(f"  Σ sheet unreal    (our computation)           {_money(sum_sheet_unreal)}")
    print(f"  Δ Unrealised                                  {_money(sum_sheet_unreal - sum_broker_profit)}")

    # ── Realised P&L: explicit gap notice ────────────────────────────────────
    print()
    print("─" * 78)
    print(" Realised P&L")
    print("─" * 78)
    print(" eToro public-API does NOT expose lifetime realised P&L.")
    print(" Truth source: Account Statement CSV (eToro → Portfolio → History → Account")
    print(" Statement → CSV), imported via `python main.py import-statement <csv>`.\n")

    closed = get_closed_positions()
    if not closed:
        print(" closed_positions table is empty.")
    else:
        total_realised = sum(float(c.get("realized_pnl") or 0) for c in closed)
        total_fees     = sum(float(c.get("fees") or 0) for c in closed)
        auto_count     = sum(1 for c in closed if c.get("source") == "auto")
        import_count   = sum(1 for c in closed if c.get("source") == "import")
        print(f" closed_positions table: {len(closed)} rows "
              f"({import_count} from statement, {auto_count} auto-detected)")
        print(f"   Σ realized_pnl  {_money(total_realised)}")
        print(f"   Σ fees          {_money(total_fees)}")

    print("\n" + "═" * 78)
    print(f" Raw response saved to: {SNAPSHOT_PATH}")
    print("═" * 78 + "\n")
