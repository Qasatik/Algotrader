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

from config.loader import config_defaults_from_argv
from core.carry_strategy import DEFAULT_TRADE_LOG, CarryConfig, CarryStrategy
from core.exchange import BybitExchange
from core.pnl_tracker import append_history as _pnl_append
from core.pnl_tracker import snapshot as _pnl_snapshot
from utils.backoff import backoff_seconds as _backoff
from utils.logger import get_logger
from utils.notifier import is_configured as _tg_configured
from utils.notifier import notify as _notify

log = get_logger("carry_runner")

_running = True


def _handle_sigint(_sig, _frame) -> None:
    global _running
    _running = False
    log.info("shutdown_requested")


def main() -> None:
    ap = argparse.ArgumentParser(description="Live funding carry runner")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=60,
                    help="poll seconds (default 60; use ~5-10 for fast basis-guard reaction)")
    ap.add_argument("--leverage", type=int, default=2)
    ap.add_argument("--equity-fraction", type=float, default=0.5)
    ap.add_argument("--basis-guard-bps", type=float, default=50.0)
    ap.add_argument("--min-funding", type=float, default=0.0001,
                    help="open when funding >= this (default 0.01%%)")
    ap.add_argument("--max-notional", type=float, default=None,
                    help="hard cap on position notional USDT (real-money safety)")
    ap.add_argument("--dry-run", action="store_true", help="decide only, no orders")
    ap.add_argument("--mainnet", action="store_true",
                    help="LIVE REAL MONEY on mainnet (requires confirmation)")
    ap.add_argument("--flatten-on-exit", action="store_true",
                    help="close the carry position on shutdown (default: leave open)")
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
                    help="polls between heartbeat Telegram messages (default 720 ≈ 1h at 5s)")
    ap.add_argument("--no-notify", action="store_true",
                    help="disable Telegram push notifications")
    ap.add_argument("--stop-loss-pct", type=float, default=15.0,
                    help="exchange-side stop-loss %% from entry (default 15%%, 0=off)")
    ap.add_argument("--max-hold-hours", type=float, default=0.0,
                    help="close position after this many hours (default 0=unlimited)")
    ap.add_argument("--pnl-log", default=None,
                    help="append a net-worth snapshot (USDT+BTC) to this CSV "
                         "every --heartbeat polls (P&L tracking)")
    ap.add_argument("--config", default=None,
                    help="path to TOML config file (overrides built-in defaults; "
                         "CLI flags still win)")
    # TOML file > built-in default; CLI flag > TOML file.
    ap.set_defaults(**config_defaults_from_argv())
    args = ap.parse_args()

    # Safety: require explicit confirmation for real-money mainnet trading.
    # --yes bypasses the prompt for automated / systemd runs.
    if args.mainnet and not args.dry_run and not args.yes:
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
        strong_funding=args.strong_funding,
        size_mult_min=args.size_mult_min,
        size_mult_max=args.size_mult_max,
        stop_loss_pct=args.stop_loss_pct,
        max_hold_hours=args.max_hold_hours,
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

    # Reconcile with the LIVE position so a restart never opens a duplicate.
    # (Skipped in dry-run, which simulates from a clean slate.)
    if not args.dry_run:
        msg = strat.reconcile()
        print(f"  reconcile: {msg}")
        log.info("carry_reconcile", result=msg)
        if msg.startswith("FAILED"):
            print("  Aborting: cannot verify position state — not trading.")
            exchange.close()
            return

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
    tg = "ON" if (_tg_configured() and not args.no_notify) else "OFF"
    print(f"\n{'=' * 60}")
    print(f"  CARRY RUNNER  | {args.symbol} | {mode}")
    print(f"  leverage {args.leverage}x | equity {args.equity_fraction:.0%} | "
          f"min funding {args.min_funding*100:.3f}% | "
          f"basis guard {args.basis_guard_bps:.0f}bps | poll {args.interval}s | "
          f"telegram {tg}")
    print(f"{'=' * 60}\n")

    # Push startup notification
    if not args.no_notify:
        _notify(f"🤖 Carry bot started | {mode} | {args.symbol} | "
                f"poll {args.interval}s | state {strat.state.value}")

    _poll_count = 0
    _consecutive_errors = 0
    act = None  # type: ignore[assignment]  # set inside loop; guarded in heartbeat
    while _running:
        try:
            act = strat.decide()
            tag = "✓" if act.action != "none" else "·"
            print(f"{tag} [{act.action:9}] funding={act.funding_rate*100:+.4f}%  "
                  f"basis={act.basis_bps:+6.1f}bps  {act.reason}")
            if not args.dry_run:
                strat.execute(act)
            _consecutive_errors = 0
            # Push notifications on significant events
            if not args.no_notify:
                if act.action == "open":
                    _notify(f"🟢 OPENED carry {act.qty:.4f} BTC | "
                            f"funding {act.funding_rate*100:+.4f}% | "
                            f"basis {act.basis_bps:+.1f}bps | {act.reason}")
                elif act.action == "close":
                    _notify(f"🔴 CLOSED carry | {act.reason} | "
                            f"funding {act.funding_rate*100:+.4f}%")
                elif act.action == "rebalance":
                    _notify(f"⚖️ Rebalanced | {act.reason} | "
                            f"basis {act.basis_bps:+.1f}bps")
        except Exception as exc:  # never let one bad poll kill the loop
            log.error("poll_failed", error=str(exc))
            print(f"✗ poll error: {exc}")
            _consecutive_errors += 1
            # Alert after 5 consecutive failures (≈25s at 5s interval)
            if _consecutive_errors == 5 and not args.no_notify:
                _notify(f"⚠️ Carry bot: 5 consecutive poll errors — last: {exc}")

        # Heartbeat cadence: every N polls, push status + log net worth
        _poll_count += 1
        _on_hb = args.heartbeat > 0 and _poll_count % args.heartbeat == 0
        if _on_hb and not args.no_notify:
            fund_str = f"funding {act.funding_rate*100:+.4f}%" if act else "funding ?"
            basis_str = f"basis {act.basis_bps:+.1f}bps" if act else "basis ?"
            _notify(f"💚 Heartbeat | {strat.state.value} | "
                    f"{fund_str} | {basis_str} | poll #{_poll_count}")
        # Log a net-worth snapshot (USDT + BTC) for P&L tracking. Independent
        # of notifications so the history builds even with --no-notify.
        if _on_hb and args.pnl_log and not args.dry_run:
            try:
                snap = _pnl_snapshot(exchange)
                if snap is not None:
                    _pnl_append(args.pnl_log, snap)
            except Exception as exc:
                log.warning("pnl_snapshot_failed", error=str(exc))

        # Sleep in small increments so SIGINT is responsive. Back off
        # exponentially on sustained errors so a down exchange isn't hammered.
        _sleep = int(_backoff(_consecutive_errors, args.interval))
        for _ in range(_sleep):
            if not _running:
                break
            time.sleep(1)

    # graceful shutdown. By default we LEAVE the carry position open so it
    # keeps collecting funding across restarts (the bot reconciles with the
    # live position on next start). Only flatten when --flatten-on-exit is set.
    if strat.state.value == "hedged" and not args.dry_run:
        if args.flatten_on_exit:
            print("\nFlattening open carry position on shutdown (--flatten-on-exit)...")
            if not args.no_notify:
                _notify("🛑 Carry bot stopping — flattening position...")
            try:
                from core.carry_strategy import CarryAction
                strat.execute(CarryAction("close", "shutdown"))
            except Exception as exc:
                log.error("shutdown_flatten_failed", error=str(exc))
                print(f"✗ failed to flatten: {exc} — CLOSE MANUALLY")
                if not args.no_notify:
                    _notify(f"🚨 FAILED to flatten on shutdown — CLOSE MANUALLY: {exc}")
        else:
            print("\nLeaving carry position OPEN (will resume on next start).")
            print("  Use --flatten-on-exit to close it on shutdown.")
            if not args.no_notify:
                _notify("🛑 Carry bot stopping — position left OPEN (will resume)")
    else:
        if not args.no_notify:
            _notify("🛑 Carry bot stopped (was FLAT)")
    exchange.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
