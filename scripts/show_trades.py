#!/usr/bin/env python3
"""Display carry trade history — local CSV log + Bybit closed-PnL.

Two sources of truth:

1. **Local log** (``data/carry_trades.csv``): every open/close/rebalance the
   bot executed, with funding rate, basis, qty and reason at decision time.
2. **Bybit closed-PnL**: the exchange's own realised-PnL records (ground truth
   for actual money made/lost on each position cycle).

Usage:
    PYTHONPATH=. python3 scripts/show_trades.py            # local log only
    PYTHONPATH=. python3 scripts/show_trades.py --bybit    # + Bybit history
    PYTHONPATH=. python3 scripts/show_trades.py --bybit --mainnet
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from core.carry_strategy import DEFAULT_TRADE_LOG
from core.exchange import BybitExchange


# ---------------------------------------------------------------------------
# Local CSV log
# ---------------------------------------------------------------------------
def _fmt_ts(ts: str) -> str:
    """Trim an ISO timestamp to a readable 'YYYY-MM-DD HH:MM' form."""
    try:
        return ts[:16].replace("T", " ")
    except (TypeError, IndexError):
        return ts


def show_local_log(path: str) -> None:
    p = Path(path)
    print(f"\n{'=' * 72}")
    print(f"  LOCAL TRADE LOG  |  {path}")
    print(f"{'=' * 72}")
    if not p.exists():
        print("  (no trades logged yet — file does not exist)")
        return

    rows: list[dict] = []
    with open(p, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("  (file exists but is empty)")
        return

    # Summary
    actions = Counter(r["action"] for r in rows)
    n_open = actions.get("open", 0)
    n_close = actions.get("close", 0)
    total_funding = sum(float(r.get("funding_rate", 0)) for r in rows if r["action"] == "open")
    print(f"  Total events: {len(rows)}  |  opens: {n_open}  closes: {n_close}")
    if n_open:
        print(f"  Avg funding at entry: {total_funding / n_open * 100:.4f}%")
    print()

    # Table (last 30 rows)
    print(f"  {'Time (UTC)':<17} {'Action':<10} {'Funding%':>9} {'Basis':>7} "
          f"{'Qty':>8}  Reason")
    print(f"  {'-' * 17} {'-' * 10} {'-' * 9} {'-' * 7} {'-' * 8}  {'-' * 24}")
    for r in rows[-30:]:
        fund = float(r.get("funding_rate", 0)) * 100
        basis = float(r.get("basis_bps", 0))
        qty = r.get("qty", "")
        print(f"  {_fmt_ts(r.get('timestamp', '')):<17} {r['action']:<10} "
              f"{fund:>+8.4f}% {basis:>+6.1f}bps {qty:>8}  {r.get('reason', '')}")


# ---------------------------------------------------------------------------
# Bybit closed-PnL
# ---------------------------------------------------------------------------
def show_bybit(symbol: str, limit: int, mainnet: bool) -> None:
    print(f"\n{'=' * 72}")
    print(f"  BYBIT CLOSED-PNL  |  {symbol}  |  {'mainnet' if mainnet else 'testnet'}")
    print(f"{'=' * 72}")
    try:
        ex = BybitExchange(testnet=False if mainnet else True)
        records = ex.get_closed_pnl(symbol=symbol, limit=limit)
        ex.close()
    except Exception as exc:
        print(f"  ✗ could not fetch: {exc}")
        return

    if not records:
        print("  (no closed positions yet)")
        return

    total_pnl = sum(float(r.get("closedPnl", 0)) for r in records)
    print(f"  Records: {len(records)}  |  Total realised PnL: {total_pnl:+.4f} USDT\n")
    print(f"  {'Time (UTC)':<17} {'Side':<5} {'Qty':>10} {'Entry':>10} {'Exit':>10} "
          f"{'PnL':>10}")
    print(f"  {'-' * 17} {'-' * 5} {'-' * 10} {'-' * 10} {'-' * 10} {'-' * 10}")
    for r in records[:limit]:
        ts = _fmt_ts(datetime.fromtimestamp(
            int(r.get("createdTime", 0)) / 1000, tz=timezone.utc
        ).isoformat()) if r.get("createdTime") else ""
        side = r.get("side", "")
        qty = r.get("closedSize", r.get("execQty", ""))
        entry = r.get("avgEntryPrice", "")
        exitp = r.get("avgExitPrice", "")
        pnl = float(r.get("closedPnl", 0))
        print(f"  {ts:<17} {side:<5} {qty:>10} {entry:>10} {exitp:>10} {pnl:>+9.4f}")


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Show carry trade history")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--log", default=DEFAULT_TRADE_LOG, help="local CSV path")
    ap.add_argument("--bybit", action="store_true", help="also fetch Bybit closed-PnL")
    ap.add_argument("--mainnet", action="store_true",
                    help="use mainnet for Bybit query (default testnet)")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    show_local_log(args.log)
    if args.bybit:
        show_bybit(args.symbol, args.limit, args.mainnet)
    print()


if __name__ == "__main__":
    main()
