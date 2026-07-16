#!/usr/bin/env python3
"""Multi-symbol delta-neutral carry runner.

Runs the carry strategy on N symbols **simultaneously**, splitting equity
equally across them.  Each symbol gets its own :class:`CarryStrategy`
instance with an auto-detected lot size (``qty_step``) queried from the
exchange, so BTC (0.001), ETH (0.01), SOL (0.1) etc. all work out of the
box.

Two operating modes
-------------------

**Fixed mode** (default) — trade a fixed list of symbols::

    PYTHONPATH=. python3 scripts/run_carry_multi.py --dry-run \\
        --symbols BTCUSDT,ETHUSDT,SOLUSDT

**Dynamic rotation mode** (``--top-n N``) — scan a candidate universe every
``--rebalance-cycles`` polls, rank by funding rate, and only allow the
top-N symbols to open new positions.  Symbols that drop out of the top-N
keep their existing hedge monitored (close / rebalance signals still fire)
but will not open again until they re-enter the top-N.  Capital is split
into N equal slots, so each rotation slot gets ``equity / N``::

    PYTHONPATH=. python3 scripts/run_carry_multi.py --mainnet --yes \\
        --top-n 3 --interval 5 --equity-fraction 0.7 --max-notional 50

Capital allocation: total USDT equity is read once at startup; each slot
receives ``total_equity / slots`` as its fixed sizing base.  The
``equity_fraction`` and ``max_notional`` caps then apply per-slot.
"""
from __future__ import annotations

import argparse
import signal
import time

from core.carry_strategy import DEFAULT_TRADE_LOG, CarryAction, CarryConfig, CarryStrategy
from core.exchange import BybitExchange
from core.pnl_tracker import append_history as _pnl_append
from core.pnl_tracker import snapshot as _pnl_snapshot
from utils.logger import get_logger
from utils.notifier import is_configured as _tg_configured
from utils.notifier import notify as _notify

log = get_logger("carry_multi")

# Candidate universe for dynamic rotation — major USDT pairs with both
# spot + perp markets on Bybit.
SCAN_UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "AVAXUSDT", "LINKUSDT", "ADAUSDT", "BNBUSDT",
    "OPUSDT", "ARBUSDT", "SUIUSDT", "APTUSDT", "NEARUSDT",
]

_running = True


def _handle_sigint(_sig, _frame) -> None:
    global _running
    _running = False
    log.info("shutdown_requested")


def _f(v, default: float = 0.0) -> float:
    """Best-effort float parse."""
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Multi-symbol funding carry runner")
    ap.add_argument("--symbols", default="BTCUSDT",
                    help="comma-separated symbols for FIXED mode "
                         "(e.g. BTCUSDT,ETHUSDT,SOLUSDT)")
    ap.add_argument("--top-n", type=int, default=0,
                    help="DYNAMIC rotation: keep only the top-N symbols by "
                         "funding rate open-eligible (0 = fixed mode)")
    ap.add_argument("--scan-symbols", default=None,
                    help="candidate universe for --top-n rotation "
                         "(default: 14 major pairs)")
    ap.add_argument("--rebalance-cycles", type=int, default=72,
                    help="polls between re-ranking in --top-n mode (default 72)")
    ap.add_argument("--interval", type=int, default=60,
                    help="poll seconds (default 60; use ~5 for fast basis-guard reaction)")
    ap.add_argument("--leverage", type=int, default=2)
    ap.add_argument("--equity-fraction", type=float, default=0.5)
    ap.add_argument("--basis-guard-bps", type=float, default=50.0)
    ap.add_argument("--min-funding", type=float, default=0.0001,
                    help="open when funding >= this (default 0.01%%)")
    ap.add_argument("--max-notional", type=float, default=None,
                    help="hard cap on position notional USDT PER SLOT (safety)")
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
    ap.add_argument("--pnl-log", default=None,
                    help="append a net-worth snapshot (USDT+BTC) to this CSV "
                         "every --heartbeat polls (P&L tracking)")
    return ap


def _make_strategy(
    exchange: BybitExchange, sym: str, args: argparse.Namespace, per_slot_equity: float,
) -> CarryStrategy:
    """Build one CarryStrategy for *sym* with an auto-detected lot step."""
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
        # Fix per-slot equity so one slot's open doesn't shrink the sizing
        # base for the others (and stays stable across rotations).
        paper_equity=per_slot_equity,
        trade_log=None if args.dry_run else DEFAULT_TRADE_LOG,
    )
    return CarryStrategy(exchange, cfg)


def _ensure_strategy(
    pool: dict[str, CarryStrategy], exchange: BybitExchange, sym: str,
    args: argparse.Namespace, per_slot_equity: float,
) -> CarryStrategy:
    """Return the strategy for *sym*, creating + reconciling it on first use."""
    strat = pool.get(sym)
    if strat is None:
        strat = _make_strategy(exchange, sym, args, per_slot_equity)
        if not args.dry_run:
            msg = strat.reconcile()
            print(f"  [{sym}] reconcile: {msg}")
            log.info("carry_reconcile", symbol=sym, result=msg)
        pool[sym] = strat
    return strat


def _scan_and_rank(
    exchange: BybitExchange, candidates: list[str], top_n: int, min_funding: float,
) -> tuple[list[str], dict[str, float]]:
    """Scan funding rates for *candidates*; return (top-N symbols, {sym: funding}).

    Only symbols with funding >= *min_funding* are eligible for the top-N.
    """
    funding_map: dict[str, float] = {}
    for sym in candidates:
        try:
            fr = exchange.get_funding_rate(sym)
            funding_map[sym] = _f(fr.get("fundingRate"))
        except Exception as exc:  # one bad symbol must not abort the scan
            log.warning("scan_symbol_failed", symbol=sym, error=str(exc))
    eligible = [(s, f) for s, f in funding_map.items() if f >= min_funding]
    eligible.sort(key=lambda x: x[1], reverse=True)
    top = [s for s, _ in eligible[:top_n]]
    return top, funding_map


def _notify_action(sym: str, act: CarryAction, no_notify: bool) -> None:
    """Push a Telegram message for meaningful actions (open/close/rebalance)."""
    if no_notify:
        return
    if act.action == "open":
        _notify(f"🟢 [{sym}] OPENED {act.qty:.4f} | funding {act.funding_rate*100:+.4f}% | "
                f"basis {act.basis_bps:+.1f}bps")
    elif act.action == "close":
        _notify(f"🔴 [{sym}] CLOSED | {act.reason}")
    elif act.action == "rebalance":
        _notify(f"⚖️ [{sym}] Rebalanced | {act.reason}")


def _confirm_mainnet(args: argparse.Namespace, symbols: list[str], n: int) -> bool:
    """Interactive REAL-MONEY confirmation gate. Returns True to proceed."""
    print("\n" + "!" * 64)
    print("  ⚠️  REAL MONEY — MAINNET LIVE TRADING  ⚠️")
    print("  This will place REAL orders with REAL funds on bybit.com.")
    cap = f" maxNotional=${args.max_notional}/slot" if args.max_notional else ""
    mode = f"top-{args.top_n} rotation" if args.top_n > 0 else f"symbols={symbols}"
    print(f"  {mode} lev={args.leverage}x equity={args.equity_fraction:.0%}{cap}")
    print("!" * 64)
    confirm = input("\n  Type 'IUNDERSTAND' to proceed: ").strip()
    return confirm == "IUNDERSTAND"


def main() -> None:
    args = _build_argparser().parse_args()

    dynamic = args.top_n > 0
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    candidates = (
        [s.strip().upper() for s in args.scan_symbols.split(",") if s.strip()]
        if args.scan_symbols else SCAN_UNIVERSE
    )
    if not dynamic and not symbols:
        print("Error: no symbols specified.")
        return
    n = args.top_n if dynamic else len(symbols)

    # Safety: require explicit confirmation for real-money mainnet trading.
    if args.mainnet and not args.dry_run and not args.yes:
        if not _confirm_mainnet(args, symbols, n):
            print("Aborted.")
            return

    signal.signal(signal.SIGINT, _handle_sigint)

    # ------------------------------------------------------------------
    # Connect & read total equity for per-slot allocation
    # ------------------------------------------------------------------
    if args.dry_run or args.mainnet:
        exchange = BybitExchange(testnet=False)  # public data (dry) / live (mainnet)
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
    per_slot_equity = total_equity / n if n > 0 else 0.0

    # ------------------------------------------------------------------
    # Strategy pool + open-eligibility flags
    # ------------------------------------------------------------------
    pool: dict[str, CarryStrategy] = {}
    can_open: dict[str, bool] = {}

    if not dynamic:
        for sym in symbols:
            _ensure_strategy(pool, exchange, sym, args, per_slot_equity)
            can_open[sym] = True
    else:
        top, fmap = _scan_and_rank(exchange, candidates, args.top_n, args.min_funding)
        for sym in top:
            _ensure_strategy(pool, exchange, sym, args, per_slot_equity)
            can_open[sym] = True
        print(f"  rotation: initial top-{args.top_n}: {', '.join(top) or '(none eligible)'}")
        log.info("carry_rotation_initial", top_n=top, funding=fmap)

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
    rot = f"top-{args.top_n} rotation" if dynamic else ", ".join(symbols)
    log.info("carry_multi_start", mode=mode, dynamic=dynamic,
             per_slot_equity=per_slot_equity, slots=n)
    print(f"\n{'=' * 64}")
    print(f"  CARRY MULTI  |  {n} slots  |  {mode}")
    print(f"  {rot}")
    print(f"  leverage {args.leverage}x | equity {args.equity_fraction:.0%} | "
          f"per-slot ${per_slot_equity:.2f} | poll {args.interval}s | telegram {tg}")
    print(f"{'=' * 64}\n")

    if not args.no_notify:
        _notify(f"🤖 Carry MULTI started | {n} slots | {mode} | {rot} | "
                f"poll {args.interval}s | ${per_slot_equity:.0f}/slot")

    # ------------------------------------------------------------------
    # Main loop — poll every strategy in the pool each cycle
    # ------------------------------------------------------------------
    _poll_count = 0
    _consecutive_errors = 0
    while _running:
        _poll_count += 1

        # --- Dynamic rotation: re-rank candidates every N polls ---------
        if dynamic and args.rebalance_cycles > 0 and _poll_count % args.rebalance_cycles == 0:
            top, fmap = _scan_and_rank(exchange, candidates, args.top_n, args.min_funding)
            for sym in list(can_open):
                can_open[sym] = sym in top
            for sym in top:
                can_open[sym] = True
                _ensure_strategy(pool, exchange, sym, args, per_slot_equity)
            active = [s for s, c in can_open.items() if c]
            hedged = [s for s, st in pool.items() if st.state.value == "hedged"]
            print(f"🔄 rotation #{_poll_count}: top-{args.top_n}={active} | "
                  f"hedged={hedged or 'none'}")
            log.info("carry_rotation", top_n=active, hedged=hedged, funding=fmap)

        # --- Poll every tracked symbol ----------------------------------
        for sym, strat in list(pool.items()):
            try:
                act = strat.decide(can_open=can_open.get(sym, False))
                tag = "✓" if act.action != "none" else "·"
                flag = "" if can_open.get(sym, True) else " [locked]"
                print(f"{tag} [{sym:8}] [{act.action:9}] "
                      f"funding={act.funding_rate*100:+.4f}%  "
                      f"basis={act.basis_bps:+6.1f}bps{flag}  {act.reason}")
                if not args.dry_run:
                    strat.execute(act)
                _consecutive_errors = 0
                _notify_action(sym, act, args.no_notify)
            except Exception as exc:  # one symbol's error must not kill the loop
                log.error("poll_failed", symbol=sym, error=str(exc))
                print(f"✗ [{sym}] poll error: {exc}")
                _consecutive_errors += 1
                if _consecutive_errors == 5 * n and not args.no_notify:
                    _notify(f"⚠️ Carry MULTI: {5*n} consecutive errors — last: {exc}")

        # Heartbeat cadence: every N polls, push status + log net worth
        _on_hb = args.heartbeat > 0 and _poll_count % args.heartbeat == 0
        if _on_hb and not args.no_notify:
            hedged = [s for s, st in pool.items() if st.state.value == "hedged"]
            _notify(f"💚 Heartbeat | {len(hedged)}/{n} hedged | "
                    f"{', '.join(hedged) if hedged else 'none'} | poll #{_poll_count}")
        # Log a net-worth snapshot (USDT + BTC) for P&L tracking.
        if _on_hb and args.pnl_log and not args.dry_run:
            try:
                snap = _pnl_snapshot(exchange)
                if snap is not None:
                    _pnl_append(args.pnl_log, snap)
            except Exception as exc:
                log.warning("pnl_snapshot_failed", error=str(exc))

        # Sleep in small increments so SIGINT is responsive
        for _ in range(args.interval):
            if not _running:
                break
            time.sleep(1)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    hedged = [(s, st) for s, st in pool.items() if st.state.value == "hedged"]
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
