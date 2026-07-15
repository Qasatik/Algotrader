#!/usr/bin/env python3
"""Multi-symbol delta-neutral carry runner.

Runs the carry strategy on N symbols **simultaneously**, splitting equity
equally across them.  Each symbol gets its own :class:`CarryStrategy`
instance with an auto-detected lot size (``qty_step``) queried from the
exchange, so BTC (0.001), ETH (0.01), SOL (0.1) etc. all work out of the
box.

Capital allocation: total USDT equity is read once at startup; each
symbol receives ``total_equity / N`` as its fixed sizing base.  The
``equity_fraction`` and ``max_notional`` caps then apply per-symbol.

Usage::

    PYTHONPATH=. python3 scripts/run_carry_multi.py --dry-run \\
        --symbols BTCUSDT,ETHUSDT,SOLUSDT

    PYTHONPATH=. python3 scripts/run_carry_multi.py --mainnet --yes \\
        --symbols BTCUSDT,ETHUSDT,SOLUSDT --interval 5 \\
        --equity-fraction 0.7 --max-notional 50 --leverage 2
"""
from __future__ import annotations

import argparse
import signal
import time

from core.carry_strategy import DEFAULT_TRADE_LOG, CarryAction, CarryConfig, CarryStrategy
from core.exchange import BybitExchange
from utils.logger import get_logger
from utils.notifier import is_configured as _tg_configured
from utils.notifier import notify as _notify

log = get_logger("carry_multi")

_running = True


def _handle_sigint(_sig, _frame) -> None:
    global _running
    _running = False
    log.info("shutdown_requested")


def main() -> None:
    ap = argparse.ArgumentParser(description="Multi-symbol funding carry runner")
    ap.add_argument("--symbols", default="BTCUSDT",
                    help="comma-separated symbols (e.g. BTCUSDT,ETHUSDT,SOLUSDT)")
    ap.add_argument("--interval", type=int, default=60,
                    help="poll seconds (default 60; use ~5 for fast basis-guard reaction)")
    ap.add_argument("--leverage", type=int, default=2)
    ap.add_argument("--equity-fraction", type=float, default=0.5)
    ap.add_argument("--basis-guard-bps", type=float, default=50.0)
    ap.add_argument("--min-funding", type=float, default=0.0001,
                    help="open when funding >= this (default 0.01%%)")
    ap.add_argument("--max-notional", type=float, default=None,
                    help="hard cap on position notional USDT PER SYMBOL (safety)")
    ap.add_argument("--dry-run", action="store_true", help="decide only, no orders")
    ap.add_argument("--mainnet", action="store_true",
                    help="LIVE REAL MONEY on mainnet (requires confirmation)")
    ap.add_argument("--flatten-on-exit", action="store_true",
                    help="close all carry positions on shutdown (default: leave open)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the interactive mainnet confirmation (for systemd/automation)")
    ap.add_argument("--paper-equity", type=float, default=10000.0,
                    help="simulated USDT equity for dry-run sizing (default 10000)")
    ap.add_argument("--strong-funding", type=float, default=0.0003,
                    help="funding rate for full-confidence sizing (default 0.03%%)")
    ap.add_argument("--size-mult-min", type=float, default=0.75,
                    help="size multiplier at zero confidence (default 0.75)")
    ap.add_argument("--size-mult-max", type=float, default=1.25,
                    help="size multiplier at full confidence (default 1.25)")
    ap.add_argument("--heartbeat", type=int, default=720,
                    help="polls between heartbeat Telegram messages (default 720)")
    ap.add_argument("--no-notify", action="store_true",
                    help="disable Telegram push notifications")
    ap.add_argument("--stop-loss-pct", type=float, default=15.0,
                    help="exchange-side stop-loss %% from entry (default 15%%, 0=off)")
    ap.add_argument("--max-hold-hours", type=float, default=0.0,
                    help="close position after this many hours (default 0=unlimited)")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("Error: no symbols specified.")
        return
    n = len(symbols)

    # Safety: require explicit confirmation for real-money mainnet trading.
    if args.mainnet and not args.dry_run and not args.yes:
        print("\n" + "!" * 64)
        print("  ⚠️  REAL MONEY — MAINNET LIVE TRADING  ⚠️")
        print("  This will place REAL orders with REAL funds on bybit.com.")
        cap = f" maxNotional=${args.max_notional}/symbol" if args.max_notional else ""
        print(f"  symbols={symbols} lev={args.leverage}x "
              f"equity={args.equity_fraction:.0%}{cap}")
        print("!" * 64)
        confirm = input("\n  Type 'IUNDERSTAND' to proceed: ").strip()
        if confirm != "IUNDERSTAND":
            print("Aborted.")
            return

    signal.signal(signal.SIGINT, _handle_sigint)

    # ------------------------------------------------------------------
    # Connect & read total equity for per-symbol allocation
    # ------------------------------------------------------------------
    if args.dry_run:
        exchange = BybitExchange(testnet=False)  # public data, no keys
    elif args.mainnet:
        exchange = BybitExchange(testnet=False)
    else:
        exchange = BybitExchange(testnet=True)

    if args.dry_run:
        total_equity = args.paper_equity
    else:
        try:
            res = exchange.get_wallet_balance("USDT")
            total_equity = float(res["list"][0]["coin"][0].get("walletBalance", 0.0))
        except Exception:
            total_equity = 0.0
    per_symbol_equity = total_equity / n if n > 0 else 0.0

    # ------------------------------------------------------------------
    # Build one CarryStrategy per symbol (auto-detect qty_step)
    # ------------------------------------------------------------------
    strategies: list[tuple[str, CarryStrategy]] = []
    for sym in symbols:
        qty_step = 0.001
        if not args.dry_run:
            qty_step = exchange.get_qty_step(sym)
            exchange.set_leverage(sym, args.leverage)
            print(f"  [{sym}] qty_step={qty_step}, leverage set")
        cfg = CarryConfig(
            symbol=sym,
            leverage=args.leverage,
            equity_fraction=args.equity_fraction,
            basis_guard_bps=args.basis_guard_bps,
            min_funding_to_open=args.min_funding,
            max_notional=args.max_notional,
            strong_funding=args.strong_funding,
            size_mult_min=args.size_mult_min,
            size_mult_max=args.size_mult_max,
            stop_loss_pct=args.stop_loss_pct,
            max_hold_hours=args.max_hold_hours,
            qty_step=qty_step,
            # Fix per-symbol equity so one symbol's open doesn't shrink
            # the sizing base for the others.
            paper_equity=(args.paper_equity / n) if args.dry_run else per_symbol_equity,
            trade_log=None if args.dry_run else DEFAULT_TRADE_LOG,
        )
        strategies.append((sym, CarryStrategy(exchange, cfg)))

    # ------------------------------------------------------------------
    # Reconcile all symbols with the LIVE exchange state
    # ------------------------------------------------------------------
    if not args.dry_run:
        for sym, strat in strategies:
            msg = strat.reconcile()
            print(f"  [{sym}] reconcile: {msg}")
            log.info("carry_reconcile", symbol=sym, result=msg)

    # ------------------------------------------------------------------
    # Banner
    # ------------------------------------------------------------------
    if args.dry_run:
        mode = "DRY-RUN"
    elif args.mainnet:
        mode = "LIVE REAL MONEY (mainnet)"
    else:
        mode = "LIVE (testnet)"
    tg = "ON" if (_tg_configured() and not args.no_notify) else "OFF"
    log.info("carry_multi_start", symbols=symbols, mode=mode,
             per_symbol_equity=per_symbol_equity)
    print(f"\n{'=' * 64}")
    print(f"  CARRY MULTI  |  {n} symbols  |  {mode}")
    print(f"  {', '.join(symbols)}")
    print(f"  leverage {args.leverage}x | equity {args.equity_fraction:.0%} | "
          f"per-symbol ${per_symbol_equity:.2f} | poll {args.interval}s | telegram {tg}")
    print(f"{'=' * 64}\n")

    if not args.no_notify:
        _notify(f"🤖 Carry MULTI started | {n} symbols: {', '.join(symbols)} | "
                f"{mode} | poll {args.interval}s | ${per_symbol_equity:.0f}/symbol")

    # ------------------------------------------------------------------
    # Main loop — poll all symbols each cycle
    # ------------------------------------------------------------------
    _poll_count = 0
    _consecutive_errors = 0
    while _running:
        for sym, strat in strategies:
            try:
                act = strat.decide()
                tag = "✓" if act.action != "none" else "·"
                print(f"{tag} [{sym:8}] [{act.action:9}] "
                      f"funding={act.funding_rate*100:+.4f}%  "
                      f"basis={act.basis_bps:+6.1f}bps  {act.reason}")
                if not args.dry_run:
                    strat.execute(act)
                _consecutive_errors = 0
                if not args.no_notify:
                    if act.action == "open":
                        _notify(f"🟢 [{sym}] OPENED {act.qty:.4f} | "
                                f"funding {act.funding_rate*100:+.4f}% | "
                                f"basis {act.basis_bps:+.1f}bps")
                    elif act.action == "close":
                        _notify(f"🔴 [{sym}] CLOSED | {act.reason}")
                    elif act.action == "rebalance":
                        _notify(f"⚖️ [{sym}] Rebalanced | {act.reason}")
            except Exception as exc:  # one symbol's error must not kill the loop
                log.error("poll_failed", symbol=sym, error=str(exc))
                print(f"✗ [{sym}] poll error: {exc}")
                _consecutive_errors += 1
                if _consecutive_errors == 5 * n and not args.no_notify:
                    _notify(f"⚠️ Carry MULTI: {5*n} consecutive errors — last: {exc}")

        # Heartbeat: every N polls, push a status line
        _poll_count += 1
        if (args.heartbeat > 0 and _poll_count % args.heartbeat == 0
                and not args.no_notify):
            hedged = [s for s, st in strategies if st.state.value == "hedged"]
            _notify(f"💚 Heartbeat | {len(hedged)}/{n} hedged | "
                    f"{', '.join(hedged) if hedged else 'none'} | poll #{_poll_count}")

        # Sleep in small increments so SIGINT is responsive
        for _ in range(args.interval):
            if not _running:
                break
            time.sleep(1)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    hedged = [(s, st) for s, st in strategies if st.state.value == "hedged"]
    if hedged and not args.dry_run:
        if args.flatten_on_exit:
            print(f"\nFlattening {len(hedged)} open position(s) (--flatten-on-exit)...")
            if not args.no_notify:
                _notify(f"🛑 Carry MULTI stopping — flattening {len(hedged)} positions...")
            for sym, strat in hedged:
                try:
                    strat.execute(CarryAction("close", "shutdown"))
                    print(f"  [{sym}] flattened ✓")
                except Exception as exc:
                    log.error("shutdown_flatten_failed", symbol=sym, error=str(exc))
                    print(f"  [{sym}] FAILED to flatten: {exc} — CLOSE MANUALLY")
                    if not args.no_notify:
                        _notify(f"🚨 [{sym}] FAILED to flatten — CLOSE MANUALLY: {exc}")
        else:
            syms = [s for s, _ in hedged]
            print(f"\nLeaving {len(hedged)} position(s) OPEN ({', '.join(syms)}).")
            print("  They will resume on next start. Use --flatten-on-exit to close.")
            if not args.no_notify:
                _notify(f"🛑 Carry MULTI stopping — {len(hedged)} positions left OPEN")
    else:
        if not args.no_notify:
            _notify("🛑 Carry MULTI stopped (all FLAT)")
    exchange.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
