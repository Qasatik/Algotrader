#!/usr/bin/env python3
"""Scan funding rates across multiple symbols to find the best carry opportunities.

Shows the current funding rate, annualised yield, basis (perp vs spot), and
8h funding income estimate for each symbol.  Symbols with high positive
funding are the best carry candidates (short perp + long spot).

Usage::

    PYTHONPATH=. python3 scripts/scan_funding.py
    PYTHONPATH=. python3 scripts/scan_funding.py --symbols BTCUSDT,ETHUSDT,SOLUSDT
"""
from __future__ import annotations

import argparse

from core.exchange import BybitExchange  # noqa: I001

# Default universe — major USDT pairs with both spot + perp on Bybit.
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "AVAXUSDT", "LINKUSDT", "ADAUSDT", "BNBUSDT",
    "OPUSDT", "ARBUSDT", "SUIUSDT", "APTUSDT", "NEARUSDT",
]


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def main() -> None:
    ap = argparse.ArgumentParser(description="Scan funding rates for carry opportunities")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated symbols (default: top 15 by volume)")
    ap.add_argument("--min-funding", type=float, default=0.0001,
                    help="highlight funding >= this (default 0.01%%)")
    args = ap.parse_args()

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols else DEFAULT_SYMBOLS
    )

    ex = BybitExchange(testnet=False)  # public data, no keys needed

    print(f"\n{'═' * 80}")
    print(f"  📡 FUNDING RATE SCANNER  |  {len(symbols)} symbols  |  "
          f"min highlight {args.min_funding*100:.3f}%/8h")
    print(f"{'═' * 80}")
    print(f"  {'Symbol':<12} {'Funding/8h':>12} {'Annualised':>12} "
          f"{'Basis bps':>10} {'Mark':>12} {'Spot':>12}")
    print(f"  {'─'*12} {'─'*12} {'─'*12} {'─'*10} {'─'*12} {'─'*12}")

    results: list[tuple[str, float, float, float, float, float]] = []
    for sym in symbols:
        try:
            fr = ex.get_funding_rate(sym)
            funding = _safe_float(fr.get("fundingRate"))
            mark = _safe_float(fr.get("markPrice")) or _safe_float(fr.get("lastPrice"))
            spot = ex.get_spot_price(sym) or mark
            basis = (mark - spot) / spot * 10_000.0 if spot > 0 else 0.0
            annual = funding * 3 * 365 * 100  # 3 funding cycles/day × 365 days
            results.append((sym, funding, annual, basis, mark, spot))
        except Exception as exc:
            print(f"  {sym:<12} {'ERROR':>12}  {exc}")
            continue

    # Sort by funding rate descending (best carry first)
    results.sort(key=lambda r: r[1], reverse=True)

    for sym, funding, annual, basis, mark, spot in results:
        # Color: green for positive funding, red for negative
        if funding >= args.min_funding:
            color, reset = "\033[32m", "\033[0m"
            marker = " ★"
        elif funding > 0:
            color, reset = "", ""
            marker = ""
        else:
            color, reset = "\033[31m", "\033[0m"
            marker = ""

        fund_str = f"{funding*100:+.4f}%"
        ann_str = f"{annual:+.1f}%"
        basis_str = f"{basis:+.1f}"
        mark_str = f"${mark:,.2f}" if mark else "—"
        spot_str = f"${spot:,.2f}" if spot else "—"

        print(f"  {sym:<12} {color}{fund_str:>12}{reset} "
              f"{color}{ann_str:>12}{reset} {basis_str:>10} "
              f"{mark_str:>12} {spot_str:>12}{marker}")

    ex.close()

    # Summary
    good = [r for r in results if r[1] >= args.min_funding]
    print(f"\n  {'─' * 76}")
    print(f"  {len(good)} symbol(s) with funding ≥ {args.min_funding*100:.3f}%/8h")
    if good:
        syms = ", ".join(r[0] for r in good)
        print(f"  → {syms}")
        avg_ann = sum(r[2] for r in good) / len(good)
        print(f"  → avg annualised yield: {avg_ann:+.1f}%")
    print(f"{'═' * 80}\n")


if __name__ == "__main__":
    main()
