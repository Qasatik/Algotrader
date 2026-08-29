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
import math
import signal
import time

from config.loader import config_defaults_from_argv
from core.btc_accumulator import BtcAccumulator
from core.carry_strategy import DEFAULT_TRADE_LOG, CarryAction, CarryConfig, CarryStrategy
from core.exchange import BybitExchange
from core.pnl_tracker import append_history as _pnl_append
from core.pnl_tracker import snapshot as _pnl_snapshot
from core.security import startup_audit as _startup_audit
from utils.backoff import backoff_seconds
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
    ap.add_argument("--min-funding", type=float, default=0.0003,
                    help="open when funding >= this (default 0.03%% — below "
                         "that the amortized round-trip fee (3.1bps/cycle) "
                         "eats the funding income)")
    ap.add_argument("--max-notional", type=float, default=None,
                    help="hard cap on position notional USDT PER SLOT (safety)")
    ap.add_argument("--min-notional", type=float, default=5.0,
                    help="skip open when notional < this USDT (Bybit spot min ~$5)")
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
    ap.add_argument("--open-fail-cooldown", type=float, default=600.0,
                    help="seconds to wait before retrying after a failed open "
                         "(default 600; prevents open/rollback churn loops)")
    ap.add_argument("--pnl-log", default="data/carry_pnl.csv",
                    help="append a net-worth snapshot (USDT+BTC) to this CSV "
                         "every --heartbeat polls (P&L tracking). Default ON — "
                         "the dashboard equity chart is dead without it "
                         "(2026-08-22: ran for a month with default=None and "
                         "the chart showed stale July data).")
    ap.add_argument("--config", default=None,
                    help="path to TOML config file (overrides built-in defaults; "
                         "CLI flags still win)")
    ap.add_argument("--skip-api-audit", action="store_true",
                    help="skip the startup API-key security audit (P3-13)")
    ap.add_argument("--btc-accum", action="store_true",
                    help="auto-convert funding profits to BTC spot (DCA into Bitcoin)")
    ap.add_argument("--btc-accum-threshold", type=float, default=5.0,
                    help="min unconverted profit (USDT) before buying BTC (default $5)")
    ap.add_argument("--btc-accum-reserve", type=float, default=10.0,
                    help="always keep this much free USDT for trading (default $10)")
    # --- Phase0: profitability upgrades ---
    ap.add_argument("--universe-auto", action="store_true",
                    help="auto-discover the scan universe: every USDT pair "
                         "with BOTH spot+perp markets, ranked by 24h turnover "
                         "(replaces the hardcoded 14-pair list)")
    ap.add_argument("--universe-size", type=int, default=60,
                    help="universe size for --universe-auto (default 60)")
    ap.add_argument("--min-turnover", type=float, default=3_000_000.0,
                    help="min 24h USDT turnover for universe candidates "
                         "(default 3M; filters illiquid tails)")
    ap.add_argument("--no-maker", action="store_true",
                    help="disable post-only maker execution (back to market orders)")
    ap.add_argument("--maker-timeout", type=float, default=45.0,
                    help="seconds to wait per leg for a maker fill before "
                         "market top-up (default 45)")
    ap.add_argument("--min-ev", type=float, default=2.0,
                    help="min entry EV in bps per 8h cycle: funding − basis − "
                         "amortized fees (default 2 = require a real edge, "
                         "not just break-even; 0 = only EV-positive entries)")
    ap.add_argument("--entry-window", type=float, default=45.0,
                    help="open only within N minutes BEFORE a funding slot "
                         "00/08/16 UTC (default 45; 0 = off)")
    ap.add_argument("--entry-blackout", type=float, default=10.0,
                    help="skip opens within N minutes AFTER a funding slot "
                         "(default 10; 0 = off)")
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
        min_notional=args.min_notional,
        strong_funding=args.strong_funding,
        size_mult_min=args.size_mult_min,
        size_mult_max=args.size_mult_max,
        stop_loss_pct=args.stop_loss_pct,
        max_hold_hours=args.max_hold_hours,
        open_fail_cooldown_s=args.open_fail_cooldown,
        qty_step=qty_step,
        maker_enabled=not args.no_maker,
        maker_timeout_s=args.maker_timeout,
        min_ev_bps=args.min_ev,
        entry_window_min=args.entry_window,
        entry_blackout_min=args.entry_blackout,
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


def _sweep_naked_spot(
    exchange: BybitExchange, universe: set[str], exclude: frozenset[str] = frozenset({"BTCUSDT"}),
) -> list[str]:
    """Sell spot coins whose perp hedge is gone (delta must stay neutral).

    2026-08-22 incident: exchange-side stop-losses closed the perp shorts
    during a rally while the spot legs stayed — ~$120 of naked alt longs sat
    unnoticed for days (in-memory state still said "hedged"). This sweep runs
    at startup and on every rotation cycle: any universe coin held on spot
    WITHOUT an open perp position is sold back to USDT.

    BTCUSDT is excluded by default — the BTC accumulator intentionally holds
    unhedged spot BTC. Returns the list of swept symbols.
    """
    sold: list[str] = []
    try:
        balances = exchange.get_all_coin_balances()
        hedged_syms = {
            p["symbol"] for p in exchange.get_positions()
            if abs(_f(p.get("size", 0))) > 0
        }
    except Exception as exc:
        log.warning("naked_spot_sweep_read_failed", error=str(exc))
        return sold
    for sym in sorted(universe - set(exclude)):
        coin = sym[:-4] if sym.endswith("USDT") else ""
        bal = balances.get(coin, 0.0)
        if not coin or bal <= 0 or sym in hedged_syms:
            continue
        try:
            lot = exchange.get_instrument_info(sym, category="spot").get("lotSizeFilter", {})
            step = float(lot.get("basePrecision", "0") or 0)
            min_qty = float(lot.get("minOrderQty", "0") or 0)
            if step <= 0:
                continue
            decimals = max(0, math.ceil(-math.log10(step))) if step < 1 else 0
            qty = round(math.floor(bal / step) * step, decimals)
            if qty < min_qty:
                continue  # dust below the exchange minimum — ignore
            exchange.place_spot_order({
                "symbol": sym, "side": "Sell", "orderType": "Market", "qty": str(qty),
            })
            sold.append(sym)
            print(f"  🧹 [{sym}] naked spot swept: sold {qty} {coin} (perp hedge was gone)")
            log.warning("naked_spot_swept", symbol=sym, qty=qty)
        except Exception as exc:
            log.warning("naked_spot_sell_failed", symbol=sym, error=str(exc))
    return sold


def _discover_universe(
    exchange: BybitExchange, size: int, min_turnover: float, keep: list[str],
) -> list[str]:
    """Auto-discover the carry universe: USDT pairs with BOTH spot + linear
    perp markets (both legs are required), ranked by 24h turnover and
    filtered for liquidity. Operator symbols (and anything currently held)
    are always kept via *keep*. Falls back to *keep* on API failure.

    Phase0: the hardcoded 14-pair list had zero selectivity — the whole
    top-14 paid the same +0.01% while tail alts print +0.1–0.3%/8h.
    """
    try:
        perp_rows = exchange.get_all_tickers("linear")
        spot_syms = {
            t.get("symbol", "") for t in exchange.get_all_tickers("spot")
            if t.get("symbol", "").endswith("USDT")
        }
        rows: list[tuple[str, float]] = []
        for t in perp_rows:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT") or sym not in spot_syms:
                continue  # carry needs BOTH legs
            turnover = _f(t.get("turnover24h"))
            if turnover < min_turnover:
                continue  # illiquid tail: wide spreads, maker traps
            rows.append((sym, turnover))
        rows.sort(key=lambda r: r[1], reverse=True)
        universe = [s for s, _ in rows[:size]]
        for s in keep:
            if s not in universe:
                universe.append(s)
        return universe
    except Exception as exc:
        log.warning("universe_discovery_failed", error=str(exc))
        return list(keep)


def _scan_and_rank(
    exchange: BybitExchange, candidates: list[str], top_n: int, min_funding: float,
    exit_cost_bps: float = 31.0, exit_hold_horizon: int = 10,
    min_ev_bps: float = 0.0,
) -> tuple[list[str], dict[str, float]]:
    """Scan candidates; return (top-N symbols by EV, {sym: EV bps}).

    Phase0 EV rotation: rank by projected per-cycle EV = funding −
    perp-discount basis − amortized round-trip fees, NOT raw funding. Two
    symbols both paying +0.01% are NOT equal if one has a −20bps perp
    discount (convergence loss) — the raw funding rank never saw that.
    ``min_funding`` stays a hard pre-filter (absolute income floor).

    P1 fix (2026-08-29): eligible symbols must ALSO clear ``min_ev_bps``.
    Before, the top-N was filtered by funding only and ranked by EV — with
    weak funding everywhere the "top-5" were simply the least-negative-EV
    symbols (logs showed −2.1…−2.6 bps), i.e. rotation kept feeding the
    per-symbol EV gate candidates that could never open profitably.
    """
    fee_bps = exit_cost_bps / max(exit_hold_horizon, 1)
    funding_map: dict[str, float] = {}
    ev_map: dict[str, float] = {}
    for sym in candidates:
        try:
            fr = exchange.get_funding_rate(sym)
            funding = _f(fr.get("fundingRate"))
            perp = _f(fr.get("markPrice")) or _f(fr.get("lastPrice"))
            spot = exchange.get_spot_price(sym) or perp or 0.0
            funding_map[sym] = funding
            if perp <= 0 or spot <= 0:
                continue
            basis_bps = (perp - spot) / spot * 10_000.0
            ev_map[sym] = funding * 10_000.0 - max(-basis_bps, 0.0) - fee_bps
        except Exception as exc:  # one bad symbol must not abort the scan
            log.warning("scan_symbol_failed", symbol=sym, error=str(exc))
    eligible = [
        (s, ev_map.get(s, funding_map[s] * 10_000.0 - fee_bps))
        for s, f in funding_map.items()
        if f >= min_funding and ev_map.get(s, -1e9) >= min_ev_bps
    ]
    eligible.sort(key=lambda x: x[1], reverse=True)
    top = [s for s, _ in eligible[:top_n]]
    return top, ev_map


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
    ap = _build_argparser()
    # TOML file > built-in default; CLI flag > TOML file.
    ap.set_defaults(**config_defaults_from_argv())
    args = ap.parse_args()

    dynamic = args.top_n > 0
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    candidates = (
        [s.strip().upper() for s in args.scan_symbols.split(",") if s.strip()]
        if args.scan_symbols else SCAN_UNIVERSE
    )
    if not dynamic and not symbols:
        print("Error: no symbols specified.")
        return
    if args.universe_auto and not dynamic:
        print("Error: --universe-auto requires --top-n rotation mode.")
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

    # P3-13: API-key security audit — confirm no withdrawal/transfer perms.
    # Only meaningful on mainnet with real credentials (dry-run uses public data).
    if args.mainnet and not args.dry_run:
        if not _startup_audit(exchange, skip=args.skip_api_audit):
            return

    if args.dry_run:
        total_equity = args.paper_equity
        free_equity = total_equity
    else:
        # P0 fix (2026-08-22 incident): a single failed wallet read at boot
        # (DNS not up yet → NameResolutionError) used to bake in $0.00/slot
        # FOREVER — equity is read once and never refreshed, so no new
        # positions could open for days while the bot looked healthy.
        # Retry with backoff; if the wallet stays unreadable, exit non-zero
        # and let systemd (Restart=always) relaunch us once the network is up.
        total_equity = 0.0
        free_equity = 0.0
        wallet_ok = False
        for attempt in range(1, 11):
            try:
                res = exchange.get_wallet_balance("USDT")
                coin = res["list"][0]["coin"][0]
                total_equity = float(coin.get("walletBalance", 0.0))
                # Margin already locked in open perp positions is NOT available for
                # new spot buys. Size from the truly free balance so existing
                # hedges don't make the bot attempt opens it can't afford
                # (Bybit rejects with "Insufficient balance" ErrCode 170131).
                locked_im = float(coin.get("totalPositionIM", 0.0))
                free_equity = max(total_equity - locked_im, 0.0)
                wallet_ok = True
                break
            except Exception as exc:
                wait = backoff_seconds(attempt, base=5.0, cap=60.0)
                print(f"  ⚠️  wallet read failed (attempt {attempt}/10): {exc} "
                      f"— retry in {wait:.0f}s")
                log.warning("startup_wallet_read_failed", attempt=attempt, error=str(exc))
                time.sleep(wait)
        if not wallet_ok:
            print("  ✖ wallet unreadable after 10 attempts — exiting; "
                  "systemd will restart us when the network is back")
            log.error("startup_wallet_read_exhausted")
            if not args.no_notify:
                _notify("🛑 Carry MULTI: wallet unreadable at startup — exiting for restart")
            raise SystemExit(3)
    per_slot_equity = free_equity / n if n > 0 else 0.0
    print(f"  capital: wallet ${total_equity:.2f} - locked margin "
          f"${total_equity - free_equity:.2f} = free ${free_equity:.2f} "
          f"→ ${per_slot_equity:.2f}/slot × {n}")
    if not args.dry_run and per_slot_equity * args.equity_fraction < args.min_notional:
        print(f"  ⚠️  free capital too low for {n} slots "
              f"(need ≥${args.min_notional / args.equity_fraction:.0f}/slot); "
              f"will monitor existing positions, new opens skipped")

    # Phase0: universe auto-discovery (needs the exchange, hence here).
    if args.universe_auto:
        keep = list(dict.fromkeys(symbols + candidates))
        candidates = _discover_universe(
            exchange, args.universe_size, args.min_turnover, keep,
        )
        print(f"  🔭 universe: auto-discovered {len(candidates)} liquid pairs "
              f"(top by turnover: {', '.join(candidates[:8])}…)")
        log.info("universe_discovered", size=len(candidates))

    # ------------------------------------------------------------------
    # BTC accumulator — auto-convert funding profits to BTC spot
    # ------------------------------------------------------------------
    accumulator: BtcAccumulator | None = None
    if args.btc_accum and args.mainnet and not args.dry_run:
        accumulator = BtcAccumulator(
            exchange,
            threshold_usdt=args.btc_accum_threshold,
            min_free_reserve=args.btc_accum_reserve,
        )
        accumulator.init_baseline()
        st = accumulator.status()
        print(f"  ₿ BTC accumulator: ON | threshold ${args.btc_accum_threshold} | "
              f"reserve ${args.btc_accum_reserve} | baseline PnL ${st['baseline_rpnl']}")
        if not args.no_notify:
            _notify(f"₿ BTC accumulator ON | threshold ${args.btc_accum_threshold} | "
                    f"reserve ${args.btc_accum_reserve}")

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
        top, fmap = _scan_and_rank(
            exchange, candidates, args.top_n, args.min_funding,
            exit_cost_bps=31.0, exit_hold_horizon=10,
            min_ev_bps=args.min_ev,
        )
        for sym in top:
            _ensure_strategy(pool, exchange, sym, args, per_slot_equity)
            can_open[sym] = True
        print(f"  rotation: initial top-{args.top_n} (by EV): "
              f"{', '.join(top) or '(none eligible)'}")
        log.info("carry_rotation_initial", top_n=top, ev_bps=fmap)

    # ------------------------------------------------------------------
    # Reconcile existing positions from a previous run (or a symbol that
    # dropped out of the top-N). They get a strategy so close/rebalance
    # signals still fire, but can_open=False so no NEW positions open.
    # ------------------------------------------------------------------
    if not args.dry_run:
        try:
            for p in exchange.get_positions():
                sym = p.get("symbol", "")
                if sym and abs(_f(p.get("size", 0))) > 0 and sym not in pool:
                    _ensure_strategy(pool, exchange, sym, args, per_slot_equity)
                    can_open[sym] = False
                    print(f"  [{sym}] existing position — monitoring (locked, no new opens)")
                    log.info("carry_reconcile_existing", symbol=sym, can_open=False)
        except Exception as exc:
            log.warning("reconcile_existing_failed", error=str(exc))

    # P0 fix (2026-08-22 incident): sell any spot coin left over from a perp
    # that was closed exchange-side (stop-loss / liquidation / manual).
    if not args.dry_run:
        swept = _sweep_naked_spot(exchange, set(candidates) | set(pool))
        if swept and not args.no_notify:
            _notify(f"🧹 Startup: naked spot swept to USDT: {', '.join(swept)}")

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

    # First PnL snapshot right away — don't wait a full heartbeat cycle
    # (~2h) before the dashboard equity chart gets a fresh point.
    if args.pnl_log and not args.dry_run:
        try:
            snap = _pnl_snapshot(exchange)
            if snap is not None:
                _pnl_append(args.pnl_log, snap)
        except Exception as exc:
            log.warning("pnl_snapshot_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Main loop — poll every strategy in the pool each cycle
    # ------------------------------------------------------------------
    _poll_count = 0
    _consecutive_errors = 0
    while _running:
        _poll_count += 1

        # --- Dynamic rotation: re-rank candidates every N polls ---------
        if dynamic and args.rebalance_cycles > 0 and _poll_count % args.rebalance_cycles == 0:
            if args.universe_auto:
                # Re-discover daily-ish (new listings gain both legs over time).
                candidates = _discover_universe(
                    exchange, args.universe_size, args.min_turnover,
                    list(dict.fromkeys(symbols + candidates)),
                )
            top, fmap = _scan_and_rank(
                exchange, candidates, args.top_n, args.min_funding,
                exit_cost_bps=31.0, exit_hold_horizon=10,
                min_ev_bps=args.min_ev,
            )
            for sym in list(can_open):
                can_open[sym] = sym in top
            for sym in top:
                can_open[sym] = True
                _ensure_strategy(pool, exchange, sym, args, per_slot_equity)
            active = [s for s, c in can_open.items() if c]
            hedged = [s for s, st in pool.items() if st.state.value == "hedged"]
            print(f"🔄 rotation #{_poll_count}: top-{args.top_n}={active} | "
                  f"hedged={hedged or 'none'}")
            log.info("carry_rotation", top_n=active, hedged=hedged, ev_bps=fmap)

            # P0 fix (2026-08-22 incident): exchange-side stop-losses can close
            # a perp at any time. The in-memory HEDGED state used to live on
            # blindly ("holding" positions that no longer existed) while the
            # naked spot leg sat unmonitored. Re-verify every hedged slot
            # against the exchange each rotation cycle, and sweep any spot
            # coin left without its perp hedge.
            if not args.dry_run:
                for sym, strat in list(pool.items()):
                    if strat.state.value != "hedged":
                        continue
                    try:
                        still_hedged = any(
                            abs(_f(p.get("size", 0))) > 0
                            for p in exchange.get_positions(sym)
                            if p.get("symbol") == sym
                        )
                        if not still_hedged:
                            msg = strat.reconcile()
                            print(f"  ↻ [{sym}] perp gone — reconcile: {msg}")
                            log.warning("carry_perp_lost", symbol=sym, result=msg)
                            if not args.no_notify:
                                _notify(f"⚠️ {sym}: perp hedge gone — {msg}")
                    except Exception as exc:
                        log.warning("loop_reconcile_failed", symbol=sym, error=str(exc))
                swept = _sweep_naked_spot(exchange, set(candidates) | set(pool))
                if swept and not args.no_notify:
                    _notify(f"🧹 Naked spot swept to USDT: {', '.join(swept)}")

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

        # BTC accumulator — sweep funding profits into BTC spot.
        # Checked every heartbeat cycle (not every poll) to reduce API calls.
        if accumulator is not None and _on_hb:
            try:
                result = accumulator.check_and_convert()
                if result and not args.no_notify:
                    _notify(
                        f"₿ BTC bought: {result.qty:.8f} BTC for ${result.usdt_spent:.2f} "
                        f"| total accumulated: {result.total_btc:.8f} BTC"
                    )
            except Exception as exc:
                log.warning("btc_accumulator_check_failed", error=str(exc))

        # Sleep so SIGINT stays responsive. When the exchange is unreachable
        # (consecutive errors), back off exponentially instead of hammering it
        # at the normal cadence — avoids rate-limiting and log/request spam.
        sleep_s = backoff_seconds(_consecutive_errors, base=args.interval)
        if sleep_s != args.interval:
            log.info("poll_backoff", seconds=sleep_s, errors=_consecutive_errors)
        slept = 0.0
        while _running and slept < sleep_s:
            time.sleep(min(1.0, sleep_s - slept))
            slept += 1.0

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
