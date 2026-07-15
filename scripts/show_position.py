#!/usr/bin/env python3
"""Live carry position monitor — P&L, funding, basis at a glance.

Shows the current open carry position (perp short + spot long) with
real-time unrealized P&L, funding income, basis, and account equity.

Usage:
    PYTHONPATH=. python3 scripts/show_position.py --mainnet          # one-shot
    PYTHONPATH=. python3 scripts/show_position.py --mainnet --watch  # auto-refresh
"""
from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from core.carry_strategy import DEFAULT_TRADE_LOG
from core.exchange import BybitExchange


def _f(v, default: float = 0.0) -> float:
    """Safe float parse."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt_usd(v: float) -> str:
    """Format a USDT amount with sign and colour."""
    if v >= 0:
        return f"\033[32m+{v:.4f} USDT\033[0m"
    return f"\033[31m{v:.4f} USDT\033[0m"


def _fmt_pct(v: float) -> str:
    """Format a percentage with sign and colour."""
    if v >= 0:
        return f"\033[32m+{v:.4f}%\033[0m"
    return f"\033[31m{v:.4f}%\033[0m"


def _load_entry(log_path: str) -> dict | None:
    """Read the most recent 'open' row from the local trade log."""
    p = Path(log_path)
    if not p.exists():
        return None
    last_open: dict | None = None
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("action") == "open":
                last_open = row
    return last_open


def _fmt_duration(seconds: float) -> str:
    """Human-readable duration: '2d 3h 15m'."""
    if seconds < 0:
        return "—"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    m = int((seconds % 3600) // 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def _fmt_next_funding(ms: float) -> str:
    """Countdown to next funding settlement."""
    now_ms = time.time() * 1000
    diff = (ms - now_ms) / 1000
    if diff <= 0:
        return "imminent"
    h = int(diff // 3600)
    m = int((diff % 3600) // 60)
    return f"{h}h {m}m"


def render(symbol: str, mainnet: bool, log_path: str) -> None:
    """Fetch and display the live carry position snapshot."""
    ex = BybitExchange(testnet=False if mainnet else True)
    try:
        # --- Perp position ---
        positions = ex.get_positions(symbol)
        perp = next((p for p in positions if p.get("symbol") == symbol), None)

        # --- Spot leg (BTC wallet balance) ---
        spot_btc = 0.0
        try:
            res = ex.get_wallet_balance("BTC")
            for c in res["list"][0]["coin"]:
                if c.get("coin") == "BTC":
                    spot_btc = _f(c.get("walletBalance"))
                    break
        except Exception:
            pass

        # --- USDT equity ---
        usdt_equity = 0.0
        try:
            res = ex.get_wallet_balance("USDT")
            coin = res["list"][0]["coin"][0]
            usdt_equity = _f(coin.get("walletBalance"))
        except Exception:
            pass

        # --- Funding + prices ---
        fr = ex.get_funding_rate(symbol)
        funding = _f(fr.get("fundingRate"))
        mark_price = _f(fr.get("markPrice")) or _f(fr.get("lastPrice"))
        next_funding_ms = _f(fr.get("nextFundingTime"))
        spot_price = ex.get_spot_price(symbol) or mark_price
    except Exception as exc:
        print(f"\n  ✗ API error: {exc}")
        ex.close()
        return
    finally:
        ex.close()

    # --- Entry info from trade log ---
    entry = _load_entry(log_path)
    entry_perp = _f(entry.get("perp_price")) if entry else 0.0
    entry_spot = _f(entry.get("spot_price")) if entry else 0.0
    open_ts = None
    if entry and entry.get("timestamp"):
        try:
            open_ts = datetime.fromisoformat(entry["timestamp"])
        except ValueError:
            pass

    # --- Compute P&L ---
    perp_size = abs(_f(perp.get("size"))) if perp else 0.0
    perp_upnl = _f(perp.get("unrealisedPnl")) if perp else 0.0
    perp_entry = _f(perp.get("avgPrice")) if perp else entry_perp
    perp_mark = mark_price or (_f(perp.get("markPrice")) if perp else 0.0)
    perp_liq = _f(perp.get("liqPrice")) if perp else 0.0
    perp_realised = _f(perp.get("curRealisedPnl")) if perp else 0.0

    # Spot unrealized P&L (long: profits when price rises)
    spot_upnl = 0.0
    if spot_btc > 0 and entry_spot > 0:
        spot_upnl = (spot_price - entry_spot) * spot_btc

    # Net delta (should be ~0 for a hedged pair)
    net_delta = spot_btc - perp_size

    # Basis
    basis_bps = 0.0
    if spot_price > 0:
        basis_bps = (perp_mark - spot_price) / spot_price * 10_000.0

    # Estimated funding income per 8h cycle (short receives when funding > 0)
    est_funding = funding * perp_size * perp_mark if perp_size > 0 else 0.0

    # Total P&L = perp uPnL + spot uPnL + realised (funding already settled)
    total_upnl = perp_upnl + spot_upnl
    total_pnl = total_upnl + perp_realised

    # Time held
    held_str = "—"
    if open_ts:
        held_str = _fmt_duration((datetime.now(timezone.utc) - open_ts).total_seconds())

    # --- Render ---
    mode = "MAINNET" if mainnet else "TESTNET"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print(f"\n{'═' * 64}")
    print(f"  📊 CARRY POSITION  |  {symbol}  |  {mode}  |  {now_str}")
    print(f"{'═' * 64}")

    if perp_size == 0 and spot_btc == 0:
        print("\n  ⬜ No open position (FLAT)\n")
        return

    state = "🟢 HEDGED" if abs(net_delta) < perp_size * 0.3 else "🟡 UNBALANCED"
    print(f"\n  Status: {state}   |   Held: {held_str}")
    print(f"  Net delta: {net_delta:+.6f} BTC"
          f"  ({'✓ balanced' if abs(net_delta) < 0.0001 else '⚠ drift'})")

    print(f"\n  {'─' * 60}")
    print("  PERPETUAL SHORT (funding collector)")
    print(f"  {'─' * 60}")
    print(f"    Size:      {perp_size:.4f} BTC")
    print(f"    Entry:     ${perp_entry:,.2f}")
    print(f"    Mark:      ${perp_mark:,.2f}")
    if perp_liq:
        print(f"    Liq:       ${perp_liq:,.2f}")
    print(f"    uPnL:      {_fmt_usd(perp_upnl)}")
    if abs(perp_realised) > 0.0001:
        print(f"    Realised:  {_fmt_usd(perp_realised)} (fees+funding)")

    print(f"\n  {'─' * 60}")
    print("  SPOT LONG (delta hedge)")
    print(f"  {'─' * 60}")
    print(f"    Size:      {spot_btc:.6f} BTC")
    if entry_spot:
        print(f"    Entry:     ${entry_spot:,.2f}")
    print(f"    Current:   ${spot_price:,.2f}")
    print(f"    uPnL:      {_fmt_usd(spot_upnl)}")

    print(f"\n  {'─' * 60}")
    print("  CARRY METRICS")
    print(f"  {'─' * 60}")
    print(f"    Funding:   {_fmt_pct(funding * 100)}/8h"
          f"  →  est. {_fmt_usd(est_funding)}/cycle")
    if next_funding_ms:
        print(f"    Next:      {_fmt_next_funding(next_funding_ms)}")
    print(f"    Basis:     {basis_bps:+.1f} bps"
          f"  ({'⚠ perp premium' if basis_bps > 30 else '✓ normal'})")

    print(f"\n  {'━' * 60}")
    print(f"  TOTAL uPnL:   {_fmt_usd(total_upnl)}")
    if abs(perp_realised) > 0.0001:
        print(f"  +Realised:    {_fmt_usd(perp_realised)} (fees+funding)")
        print(f"  = NET P&L:    {_fmt_usd(total_pnl)}")
    print(f"  Account:      {usdt_equity:.2f} USDT")
    print(f"{'═' * 64}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Live carry position monitor")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--mainnet", action="store_true", help="use mainnet (default testnet)")
    ap.add_argument("--log", default=DEFAULT_TRADE_LOG, help="local CSV trade log path")
    ap.add_argument("--watch", type=float, default=0, metavar="SECS",
                    help="auto-refresh every N seconds (default: one-shot)")
    args = ap.parse_args()

    if args.watch > 0:
        try:
            while True:
                os.system("clear" if os.name != "nt" else "cls")
                render(args.symbol, args.mainnet, args.log)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n  Stopped.")
    else:
        render(args.symbol, args.mainnet, args.log)


if __name__ == "__main__":
    main()
