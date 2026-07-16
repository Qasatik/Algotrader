#!/usr/bin/env python3
"""Carry bot P&L dashboard — net worth in USDT *and* BTC.

Snapshots the unified account's mark-to-market equity, appends it to a
history CSV, and prints a dashboard: current net worth, P&L since the
first snapshot (baseline), and annualised yield (APR) in both USDT and
BTC terms.

The BTC-denominated figure is the real "accumulate BTC" metric: it rises
only when USDT equity grows faster than BTC appreciates. If BTC moons
while the bot earns USDT funding, this number can still go down — that is
the honest signal for whether the strategy is building BTC wealth.

Usage::

    PYTHONPATH=. python3 scripts/show_pnl.py --mainnet
    PYTHONPATH=. python3 scripts/show_pnl.py --mainnet --reset     # new baseline
    PYTHONPATH=. python3 scripts/show_pnl.py --mainnet --history   # trend table
    PYTHONPATH=. python3 scripts/show_pnl.py --mainnet --no-snapshot  # read-only
"""
from __future__ import annotations

import argparse

from core.exchange import BybitExchange
from core.pnl_tracker import (
    DEFAULT_PNL_LOG,
    NetWorth,
    append_history,
    load_history,
    reset_baseline,
    snapshot,
    summary,
)


def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def _fmt_btc(v: float) -> str:
    return f"₿{v:.8f}"


def _print_dashboard(snap: NetWorth, hist: list[NetWorth]) -> None:
    print(f"\n{'═' * 60}")
    print(f"📈  CARRY P&L  |  {snap.timestamp}")
    print(f"{'═' * 60}")
    print(f"  Account equity : {_fmt_usd(snap.equity_usdt)}")
    print(f"  BTC price      : {_fmt_usd(snap.btc_price)}")
    print(f"  Net worth      : {_fmt_btc(snap.equity_btc)}  (= equity ÷ BTC price)")
    s = summary(hist)
    if "first" not in s:
        print("\n  (baseline set — run again later to see P&L)")
        print(f"{'═' * 60}\n")
        return
    days = s["years"] * 365.25
    print(f"{'─' * 60}")
    print(f"  Since {s['first'].timestamp}  ({s['n']} snaps, {days:.2f} days)")
    print(f"  USDT P&L : {s['delta_usdt']:+,.2f}  ({s['pct_usdt']:+.2f}%)  "
          f"APR {s['apr_usdt']:+.1f}%")
    print(f"  BTC  P&L : {s['delta_btc']:+.8f}  ({s['pct_btc']:+.2f}%)  "
          f"APR {s['apr_btc']:+.1f}%")
    print(f"{'═' * 60}\n")


def _print_history(hist: list[NetWorth]) -> None:
    if not hist:
        print("No history yet.")
        return
    print(f"\n{'Time':<26}{'Equity USDT':>14}{'BTC price':>14}{'Net worth BTC':>18}")
    print("─" * 72)
    for h in hist[-20:]:
        print(f"{h.timestamp:<26}{h.equity_usdt:>14,.2f}{h.btc_price:>14,.2f}"
              f"{h.equity_btc:>18,.8f}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Carry bot P&L dashboard")
    ap.add_argument("--mainnet", action="store_true", help="use mainnet (real account)")
    ap.add_argument("--pnl-log", default=DEFAULT_PNL_LOG, help="history CSV path")
    ap.add_argument("--reset", action="store_true",
                    help="start a new baseline (keep only the latest snapshot)")
    ap.add_argument("--history", action="store_true", help="print the trend table")
    ap.add_argument("--no-snapshot", action="store_true",
                    help="don't append a new snapshot (read-only)")
    args = ap.parse_args()

    exchange = BybitExchange(testnet=False if args.mainnet else True)

    if args.reset:
        last = reset_baseline(args.pnl_log)
        if last is not None:
            print(f"Baseline reset to {last.timestamp} ({last.equity_usdt:.2f} USDT, "
                  f"{last.equity_btc:.8f} BTC).")
        else:
            print("No history to reset.")

    if not args.no_snapshot:
        snap = snapshot(exchange)
        if snap is None:
            print("Could not read account equity (no balance / not connected).")
            exchange.close()
            return
        append_history(args.pnl_log, snap)

    hist = load_history(args.pnl_log)
    if args.history:
        _print_history(hist)
    elif hist:
        _print_dashboard(hist[-1], hist)
    exchange.close()


if __name__ == "__main__":
    main()
