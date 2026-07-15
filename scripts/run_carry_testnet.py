#!/usr/bin/env python3
"""Run the live delta-neutral carry strategy on Bybit (testnet by default).

Polls the funding rate + basis every ``--interval`` seconds and lets the
:class:`CarryStrategy` state machine decide: open the short-perp + long-spot
pair when funding is favorable, flatten on a basis blowout, rebalance on drift.

Use ``--dry-run`` first to see decisions without placing orders.

Usage:
    PYTHONPATH=. python3 scripts/run_carry_testnet.py --dry-run
    PYTHONPATH=. python3 scripts/run_carry_testnet.py --interval 300
"""
from __future__ import annotations

import argparse
import signal
import time

from core.carry_strategy import DEFAULT_TRADE_LOG, CarryConfig, CarryStrategy
from core.exchange import BybitExchange
from utils.logger import get_logger

log = get_logger("carry_runner")

_running = True


def _handle_sigint(_sig, _frame) -> None:
    global _running
    _running = False
    log.info("shutdown_requested")


def main() -> None:
    ap = argparse.ArgumentParser(description="Live funding carry runner")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=300, help="poll seconds (default 300)")
    ap.add_argument("--leverage", type=int, default=2)
    ap.add_argument("--equity-fraction", type=float, default=0.5)
    ap.add_argument("--basis-guard-bps", type=float, default=50.0)
    ap.add_argument("--min-funding", type=float, default=0.0001,
                    help="open when funding >= this (default 0.01%)")
    ap.add_argument("--max-notional", type=float, default=None,
                    help="hard cap on position notional USDT (real-money safety)")
    ap.add_argument("--dry-run", action="store_true", help="decide only, no orders")
    ap.add_argument("--mainnet", action="store_true",
                    help="LIVE REAL MONEY on mainnet (requires confirmation)")
    ap.add_argument("--paper-equity", type=float, default=10000.0,
                    help="simulated USDT equity for dry-run sizing (default 10000)")
    args = ap.parse_args()

    # Safety: require explicit confirmation for real-money mainnet trading.
    if args.mainnet and not args.dry_run:
        print("\n" + "!" * 64)
        print("  ⚠️  REAL MONEY — MAINNET LIVE TRADING  ⚠️")
        print("  This will place REAL orders with REAL funds on bybit.com.")
        cap = f" maxNotional=${args.max_notional}" if args.max_notional else ""
        print(f"  symbol={args.symbol} lev={args.leverage}x equity={args.equity_fraction:.0%}{cap}")
        print("!" * 64)
        confirm = input("\n  Type 'IUNDERSTAND' to proceed: ").strip()
        if confirm != "IUNDERSTAND":
            print("Aborted.")
            return

    signal.signal(signal.SIGINT, _handle_sigint)

    cfg = CarryConfig(
        symbol=args.symbol,
        leverage=args.leverage,
        equity_fraction=args.equity_fraction,
        basis_guard_bps=args.basis_guard_bps,
        min_funding_to_open=args.min_funding,
        max_notional=args.max_notional,
        paper_equity=args.paper_equity if args.dry_run else None,
        # Always log trades to CSV (local persistent history) unless dry-run.
        trade_log=None if args.dry_run else DEFAULT_TRADE_LOG,
    )
    if args.dry_run:
        # Dry-run reads PUBLIC mainnet data (real funding rates, no keys needed).
        exchange = BybitExchange(testnet=False)
    elif args.mainnet:
        # LIVE REAL MONEY on the main Bybit exchange.
        exchange = BybitExchange(testnet=False)
        exchange.set_leverage(args.symbol, args.leverage)
    else:
        # Default live mode uses the demo/testnet environment (no real money).
        exchange = BybitExchange(testnet=True)
        exchange.set_leverage(args.symbol, args.leverage)
    strat = CarryStrategy(exchange, cfg)

    if args.dry_run:
        mode = "DRY-RUN"
    elif args.mainnet:
        mode = "LIVE REAL MONEY (mainnet)"
    else:
        mode = "LIVE (testnet)"
    log.info(
        "carry_start", symbol=args.symbol, leverage=args.leverage,
        equity_fraction=args.equity_fraction, mode=mode,
        basis_guard_bps=args.basis_guard_bps, min_funding=args.min_funding,
    )
    print(f"\n{'=' * 60}")
    print(f"  CARRY RUNNER  | {args.symbol} | {mode}")
    print(f"  leverage {args.leverage}x | equity {args.equity_fraction:.0%} | "
          f"min funding {args.min_funding*100:.3f}% | "
          f"basis guard {args.basis_guard_bps:.0f}bps | poll {args.interval}s")
    print(f"{'=' * 60}\n")

    while _running:
        try:
            act = strat.decide()
            tag = "✓" if act.action != "none" else "·"
            print(f"{tag} [{act.action:9}] funding={act.funding_rate*100:+.4f}%  "
                  f"basis={act.basis_bps:+6.1f}bps  {act.reason}")
            if not args.dry_run:
                strat.execute(act)
        except Exception as exc:  # never let one bad poll kill the loop
            log.error("poll_failed", error=str(exc))
            print(f"✗ poll error: {exc}")
        # sleep in small increments so SIGINT is responsive
        for _ in range(args.interval):
            if not _running:
                break
            time.sleep(1)

    # graceful shutdown: flatten if still hedged
    if strat.state.value == "hedged" and not args.dry_run:
        print("\nFlattening open carry position on shutdown...")
        try:
            from core.carry_strategy import CarryAction
            strat.execute(CarryAction("close", "shutdown"))
        except Exception as exc:
            log.error("shutdown_flatten_failed", error=str(exc))
            print(f"✗ failed to flatten: {exc} — CLOSE MANUALLY")
    exchange.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
