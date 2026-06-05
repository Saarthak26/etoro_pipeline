"""
statement_importer.py — Import closed trade history from an eToro account statement.

How to export from eToro:
  1. Log in to eToro → Portfolio → History
  2. Click "Account Statement" → set date range → Export
  3. Run: python main.py import-statement ~/Downloads/etoro-account-statement.xlsx

Two formats supported:
  • .xlsx — eToro's native Excel export. Reads the "Closed Positions" sheet,
            which carries open/close prices, dates, broker-authoritative P&L
            in USD, spread fees, and overnight/dividend amounts.
  • .csv  — Account Activity export. Reads rows where Type == "Position closed".
            Less rich (no open price / open date) but works when .xlsx isn't
            available.

Either path writes into the closed_positions table.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import sys

log = logging.getLogger(__name__)

# Matches "Company Name (TICKER)" or "Company (TICKER.EX)" — pulls the symbol in parens at end.
_TICKER_FROM_ACTION = re.compile(r"\(([^)]+)\)\s*$")


def _parse_number(s: str) -> float:
    """Parse a number string that may contain commas, currency symbols, or be empty."""
    if not s:
        return 0.0
    try:
        return float(s.replace(",", "").replace("$", "").replace(" ", "").strip())
    except ValueError:
        return 0.0


def _parse_etoro_date(s: str) -> str:
    """
    Convert eToro date formats to ISO YYYY-MM-DD.
    eToro uses: DD/MM/YYYY HH:MM:SS  or  YYYY-MM-DD HH:MM:SS
    """
    if not s:
        return ""
    s = s.strip()
    # Try DD/MM/YYYY HH:MM:SS
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s[:10]  # best-effort: first 10 chars


def import_etoro_statement(path: str) -> int:
    """
    Parse an eToro account statement (.xlsx or .csv) and upsert all closed
    positions into the closed_positions table. Returns the number of records
    imported.
    """
    from database import save_closed_position, initialise_db

    if not os.path.exists(path):
        print(f"ERROR: File not found: {path}")
        sys.exit(1)

    initialise_db()

    if path.lower().endswith((".xlsx", ".xls")):
        return _import_xlsx(path)

    return _import_csv(path)


def _import_xlsx(xlsx_path: str) -> int:
    """Import closed trades from the 'Closed Positions' sheet of an .xlsx statement."""
    from database import save_closed_position

    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas is required to import .xlsx statements. Install with: pip install pandas openpyxl")
        sys.exit(1)

    try:
        df = pd.read_excel(xlsx_path, sheet_name="Closed Positions")
    except ValueError as e:
        print(f"ERROR: 'Closed Positions' sheet not found in {xlsx_path}: {e}")
        print("Make sure the file is the full eToro Account Statement export.")
        sys.exit(1)

    required = ["Position ID", "Action", "Long / Short", "Units / Contracts",
                "Open Date", "Close Date", "Open Rate", "Close Rate", "Profit(USD)"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"ERROR: 'Closed Positions' sheet is missing columns: {missing}")
        sys.exit(1)

    count = 0
    skipped = 0
    total_realised = 0.0
    total_fees = 0.0

    for _, row in df.iterrows():
        pid = str(row.get("Position ID") or "").strip()
        action = str(row.get("Action") or "").strip()
        m = _TICKER_FROM_ACTION.search(action)
        if not pid or not m:
            skipped += 1
            continue

        ticker = m.group(1).strip().upper()
        # Drop exchange suffix on tickers like "STX.US" / "IDR.MC" — keep the base symbol
        # so per-ticker tabs and watchlist lookups still resolve.
        if "." in ticker:
            ticker = ticker.split(".", 1)[0]

        direction = "BUY" if str(row.get("Long / Short") or "Long").strip().lower() == "long" else "SELL"
        units = abs(_to_float(row.get("Units / Contracts")))
        open_price = _to_float(row.get("Open Rate"))
        close_price = _to_float(row.get("Close Rate"))
        realised = _to_float(row.get("Profit(USD)"))
        spread = _to_float(row.get("Spread Fees (USD)"))
        open_date = _parse_etoro_date(str(row.get("Open Date") or ""))
        close_date = _parse_etoro_date(str(row.get("Close Date") or ""))

        save_closed_position({
            "position_id": pid,
            "ticker":      ticker,
            "direction":   direction,
            "units":       units,
            "open_price":  open_price,
            "open_date":   open_date,
            "close_price": close_price,
            "close_date":  close_date,
            "realized_pnl": realised,
            "fees":         spread,
            "source":       "import",
        })
        count += 1
        total_realised += realised
        total_fees += spread

    print(f"Imported {count} closed positions from xlsx ({skipped} skipped — missing Position ID or ticker).")
    print(f"  Σ realised P&L: ${total_realised:,.2f}")
    print(f"  Σ spread fees:  ${total_fees:,.2f}")
    if count > 0:
        print("Run 'python main.py export' to refresh the dashboard.")
    return count


def _to_float(v) -> float:
    """Coerce a pandas/Excel value to float, treating NaN/None/empty as 0.0."""
    try:
        import pandas as pd
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return 0.0
    except ImportError:
        if v is None:
            return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return _parse_number(str(v))


def _import_csv(csv_path: str) -> int:
    """Legacy CSV path — parses Account Activity rows where Type == 'Position closed'."""
    from database import save_closed_position

    count = 0
    skipped = 0

    # Try multiple encodings — eToro exports vary
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(csv_path, encoding=encoding, newline="") as f:
                # Peek at headers to confirm format
                sample = f.read(512)
                f.seek(0)
                if "Position ID" not in sample and "Asset name" not in sample:
                    # Try to handle eToro's Excel export which may have extra header rows
                    lines = f.readlines()
                    # Find the header row
                    header_idx = next(
                        (i for i, l in enumerate(lines) if "Position ID" in l or "Asset name" in l),
                        None,
                    )
                    if header_idx is None:
                        print(f"ERROR: Could not find 'Position ID' column in {csv_path}.")
                        print("Make sure you exported using Account Statement → CSV/Excel from eToro.")
                        sys.exit(1)
                    lines = lines[header_idx:]
                    content = "".join(lines)
                    import io
                    reader = csv.DictReader(io.StringIO(content))
                else:
                    reader = csv.DictReader(f)

                for row in reader:
                    row_type = (row.get("Type") or row.get("type") or "").strip()
                    if row_type != "Position closed":
                        continue

                    pid      = (row.get("Position ID") or "").strip()
                    asset    = (row.get("Asset name") or "").strip().upper()
                    realized = _parse_number(row.get("Realized Equity Change") or "0")
                    units    = abs(_parse_number(row.get("Units") or "0"))
                    close_date = _parse_etoro_date(row.get("Date") or "")

                    if not pid:
                        skipped += 1
                        continue

                    # Generate a synthetic position_id if one is missing
                    pos_id = pid or f"import-{asset}-{close_date}-{count}"

                    save_closed_position({
                        "position_id": pos_id,
                        "ticker":      asset,
                        "direction":   "BUY",   # eToro statement doesn't distinguish in this field
                        "units":       units,
                        "open_price":  0,        # not available in statement CSV
                        "open_date":   "",
                        "close_price": 0,
                        "close_date":  close_date,
                        "realized_pnl": realized,
                        "fees":        0,
                        "source":      "import",
                    })
                    count += 1

            break  # Successfully read with this encoding
        except UnicodeDecodeError:
            continue

    print(f"Imported {count} closed position record(s) ({skipped} skipped — missing Position ID).")
    if count > 0:
        print("Run 'python main.py export' to update the Monthly Performance and Closed Trades tabs.")
    return count
