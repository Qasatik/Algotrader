"""Live delta-neutral funding carry strategy.

State machine that runs the passive carry trade against the live Bybit API:

    FLAT  ──funding≥min──▶  HEDGED  ──basis blowout──▶  FLAT
                              │  │
                              │  └─drift>rebalance─▶ REBALANCE (stay HEDGED)
                              └─collects funding every 8h while HEDGED

When HEDGED the position is **short perpetual + long spot** (delta-neutral):
price moves cancel out and we harvest the positive funding cash flow. The
**basis guard** is the critical safety — if the perpetual trades far above spot
(a short squeeze), the perp leg can be liquidated before the spot hedge is
sold, so we flatten preemptively.

Design: ``decide()`` is pure logic (reads market data, returns a
:class:`CarryAction`, updates internal state) and is fully unit-testable with a
mock exchange. ``execute()`` turns an action into real orders.
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from core.exchange import BybitExchange
from utils.logger import get_logger

log = get_logger("carry_strategy")

# Default location for the persistent trade log (CSV).
DEFAULT_TRADE_LOG = "data/carry_trades.csv"
TRADE_LOG_FIELDS = [
    "timestamp", "action", "symbol", "side", "qty", "funding_rate",
    "basis_bps", "perp_price", "spot_price", "confidence", "reason",
]


class CarryState(str, Enum):
    """Lifecycle of the carry position."""

    FLAT = "flat"  # no position, scanning for entry
    HEDGED = "hedged"  # short perp + long spot, collecting funding


@dataclass
class CarryConfig:
    """Conservative defaults for the live carry strategy."""

    symbol: str = "BTCUSDT"
    leverage: int = 2  # perp leverage (≤2× = conservative)
    equity_fraction: float = 0.5  # notional = fraction × equity (see sizing note)
    min_funding_to_open: float = 0.0001  # open when funding ≥ 0.01%
    close_funding: float = -0.0001  # consider exit when funding < this (< -0.01%)
    # EV-gated exit: don't close on mild/transient negative funding — only when
    # the projected holding loss exceeds the round-trip close cost.
    exit_cost_bps: float = 31.0  # round-trip fee to close (perp+spot taker+slippage)
    exit_hold_horizon: int = 10  # funding cycles (8h) projected: hold-vs-close break-even
    exit_confirm_polls: int = 3  # consecutive warranted polls before closing (anti-churn)
    basis_guard_bps: float = 50.0  # flatten if perp premium > 50 bps (0.5%)
    rebalance_drift_bps: float = 20.0  # rebalance hedge if basis drifts > 20 bps
    rebalance_min_btc: float = 0.001  # min BTC mismatch to trigger a corrective order
    # Conviction-weighted sizing: scale size with entry confidence (P-profit proxy).
    strong_funding: float = 0.0003  # funding rate (0.03%) at which confidence = 1.0
    size_mult_min: float = 0.75  # size multiplier at zero confidence
    size_mult_max: float = 1.25  # size multiplier at full confidence
    qty_step: float = 0.001  # BTC lot step (round qty down to this)
    paper_equity: float | None = None  # if set, override wallet balance (dry-run)
    max_notional: float | None = None  # hard cap on position notional (USDT safety)
    trade_log: str | None = None  # CSV path for persistent trade history (None=off)
    # Exchange-side backstop stop-loss (protects while bot is OFFLINE).
    # Set as a Bybit trading-stop order on the perp short after opening.
    # 0 = disabled.  For a short, SL triggers when price RISES by this %.
    stop_loss_pct: float = 15.0  # close perp if price rises 15% (well below 2× liq)
    liq_warning_pct: float = 15.0  # alert if mark price within 15% of liq price
    # Max hold time: close the position after this many hours regardless of
    # funding (prevents holding a decaying position forever). 0 = unlimited.
    max_hold_hours: float = 0.0

    @property
    def base_coin(self) -> str:
        """Base currency derived from symbol (BTCUSDT → BTC, ETHUSDT → ETH)."""
        return self.symbol.replace("USDT", "")


@dataclass
class CarryAction:
    """Next action produced by :meth:`CarryStrategy.decide`."""

    action: str  # "open" | "close" | "rebalance" | "none"
    reason: str
    perp_side: str | None = None  # "Sell" (short) when opening
    spot_side: str | None = None  # "Buy" (long) when opening
    qty: float = 0.0
    funding_rate: float = 0.0
    basis_bps: float = 0.0
    perp_price: float = 0.0
    spot_price: float = 0.0
    confidence: float = 0.0  # entry conviction in [0, 1] (P-profit proxy)


def _basis_bps(perp_price: float, spot_price: float) -> float:
    """Perpetual premium over spot, in basis points (positive = perp dearer)."""
    if spot_price <= 0:
        return 0.0
    return (perp_price - spot_price) / spot_price * 10_000.0


class CarryStrategy:
    """Delta-neutral funding carry with basis-guard risk control."""

    def __init__(self, exchange: BybitExchange, cfg: CarryConfig | None = None) -> None:
        self.exchange = exchange
        self.cfg = cfg or CarryConfig()
        self.state = CarryState.FLAT
        self.position_qty: float = 0.0
        self.entry_basis_bps: float = 0.0
        self._exit_signals: int = 0  # consecutive EV-warranted exit polls
        self._entry_time: float | None = None  # epoch seconds when position opened
        self._poll_count: int = 0  # total decide() calls (for throttling checks)

    # ------------------------------------------------------------------
    # Startup reconciliation (P0-1)
    # ------------------------------------------------------------------
    def reconcile(self) -> str:
        """Sync internal state with the LIVE exchange position on startup.

        Without this, a restart would think the bot is FLAT and open a
        DUPLICATE position on top of an existing one. Call once before the
        trading loop. Returns a status string (prefix ``"FAILED"`` ⇒ do not
        trade — the runner aborts in that case).
        """
        try:
            positions = self.exchange.get_positions(self.cfg.symbol)
        except Exception as exc:
            log.error("carry_reconcile_read_failed", error=str(exc))
            return "FAILED to read positions — staying FLAT, will NOT trade"
        perp_size = 0.0
        for p in positions:
            if p.get("symbol") == self.cfg.symbol:
                perp_size = abs(_safe_float(p.get("size")))
                break
        spot_size = 0.0
        try:
            res = self.exchange.get_wallet_balance(self.cfg.base_coin)
            for c in res["list"][0]["coin"]:
                if c.get("coin") == self.cfg.base_coin:
                    spot_size = _safe_float(c.get("walletBalance"))
                    break
        except Exception:  # non-fatal: treat the spot leg as absent
            pass
        # Existing hedged pair (spot covers >= half the perp) → resume it.
        if perp_size > 0 and spot_size >= perp_size * 0.5:
            self.state = CarryState.HEDGED
            self.position_qty = perp_size
            fr = self.exchange.get_funding_rate(self.cfg.symbol)
            mark = _safe_float(fr.get("markPrice")) or _safe_float(fr.get("lastPrice"))
            spot = self.exchange.get_spot_price(self.cfg.symbol) or 0.0
            self.entry_basis_bps = _basis_bps(mark, spot)
            # Re-verify the exchange-side stop-loss is in place after restart.
            self._set_exchange_stop_loss(mark)
            self._entry_time = time.time()  # reset hold timer on resume
            log.info("carry_reconcile_hedged", perp=perp_size, spot=spot_size)
            return f"resumed HEDGED position (perp {perp_size}, spot {spot_size} BTC)"
        # Orphaned perp short with no hedge = NAKED SHORT (dangerous) → flatten.
        if perp_size > 0:
            log.error("carry_reconcile_orphan_perp", perp_size=perp_size, spot_size=spot_size)
            self._emergency_close_perp(perp_size)
            self.state = CarryState.FLAT
            self.position_qty = 0.0
            return f"FLATTENED orphaned perp short {perp_size} (was unhedged!)"
        # Leftover spot long (dust from a partial close) — not dangerous; warn.
        if spot_size > 0:
            log.warning("carry_reconcile_orphan_spot", spot_size=spot_size)
            self.state = CarryState.FLAT
            return f"flat; note: leftover spot long {spot_size} BTC (ignored)"
        self.state = CarryState.FLAT
        log.info("carry_reconcile_flat")
        return "flat (no open position)"

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------
    def _equity_usdt(self) -> float:
        """Available USDT equity (best-effort; 0 if unreadable)."""
        if self.cfg.paper_equity is not None:
            return self.cfg.paper_equity
        try:
            res = self.exchange.get_wallet_balance("USDT")
            coin = res["list"][0]["coin"][0]
            return float(coin.get("walletBalance", 0.0))
        except (KeyError, IndexError, TypeError, ValueError):
            return 0.0

    def _position_size(self, price: float, confidence: float = 1.0) -> float:
        """Conservative notional → quantity, scaled by entry confidence.

        notional = equity_fraction × equity × size_multiplier(confidence).
        ``size_multiplier`` ranges [size_mult_min, size_mult_max] so a
        high-confidence entry (strong funding, low basis) deploys more capital
        and a marginal one less. ``max_notional`` is a HARD cap applied *after*
        scaling — conviction can never breach it. Quantity is rounded down to
        the exchange lot step.
        """
        equity = self._equity_usdt()
        notional = equity * self.cfg.equity_fraction * self._size_multiplier(confidence)
        if self.cfg.max_notional is not None:
            notional = min(notional, self.cfg.max_notional)  # hard safety cap
        raw_qty = notional / price if price > 0 else 0.0
        step = self.cfg.qty_step
        return max((raw_qty // step) * step, 0.0)

    def _entry_confidence(self, funding: float, basis: float) -> float:
        """Heuristic P(profit) proxy in [0, 1] for a carry entry.

        Two drivers:
        * **funding cushion** — yield above the entry floor; more cushion over
          the round-trip fee break-even ⇒ more likely to net positive.
        * **basis safety** — basis near the guard ⇒ short-squeeze risk ⇒ less
          confidence. Negative basis (perp below spot) is *not* penalised.
        """
        span = self.cfg.strong_funding - self.cfg.min_funding_to_open
        funding_score = 0.0 if span <= 0 else (funding - self.cfg.min_funding_to_open) / span
        funding_score = max(0.0, min(1.0, funding_score))
        basis_risk = (
            max(0.0, min(1.0, basis / self.cfg.basis_guard_bps))
            if self.cfg.basis_guard_bps > 0 else 0.0
        )
        return funding_score * (1.0 - basis_risk)

    def _size_multiplier(self, confidence: float) -> float:
        """Map confidence [0, 1] → size multiplier [size_mult_min, size_mult_max]."""
        c = max(0.0, min(1.0, confidence))
        return self.cfg.size_mult_min + (self.cfg.size_mult_max - self.cfg.size_mult_min) * c

    # ------------------------------------------------------------------
    # Decision logic (pure / testable)
    # ------------------------------------------------------------------
    def decide(self) -> CarryAction:
        """Poll funding + basis and return the next action.

        Updates ``self.state`` / ``self.position_qty`` as a side effect so the
        strategy is stateful across calls.
        """
        self._poll_count += 1
        fr = self.exchange.get_funding_rate(self.cfg.symbol)
        funding = _safe_float(fr.get("fundingRate"))
        perp_price = _safe_float(fr.get("markPrice")) or _safe_float(fr.get("lastPrice"))
        spot_price = self.exchange.get_spot_price(self.cfg.symbol) or perp_price or 0.0
        basis = _basis_bps(perp_price, spot_price)

        if self.state == CarryState.HEDGED:
            act = self._decide_hedged(funding, basis)
        else:
            act = self._decide_flat(funding, basis, perp_price or spot_price)
        act.perp_price = perp_price
        act.spot_price = spot_price
        return act

    def _decide_hedged(self, funding: float, basis: float) -> CarryAction:
        # 0) Liquidation proximity warning (side-effect only — logs/alerts).
        self._check_liq_proximity()
        # 1) Basis guard — short-squeeze liquidation protection (highest priority).
        if basis > self.cfg.basis_guard_bps:
            self.state = CarryState.FLAT
            self.position_qty = 0.0
            return CarryAction(
                "close",
                f"basis guard {basis:.0f}bps > {self.cfg.basis_guard_bps:.0f}bps",
                funding_rate=funding,
                basis_bps=basis,
            )
        # 2) EV-gated exit: only close when the projected holding loss from
        #    continuing to pay negative funding exceeds the round-trip close
        #    cost. This avoids churning out on transient/mild negative funding
        #    where paying the close fee would cost more than riding it out.
        if funding < self.cfg.close_funding:
            projected_loss_bps = abs(funding) * self.cfg.exit_hold_horizon * 10_000.0
            if projected_loss_bps > self.cfg.exit_cost_bps:
                self._exit_signals += 1
                if self._exit_signals >= self.cfg.exit_confirm_polls:
                    self._exit_signals = 0
                    self.state = CarryState.FLAT
                    self.position_qty = 0.0
                    return CarryAction(
                        "close",
                        f"EV exit: hold-loss {projected_loss_bps:.0f}bps > "
                        f"close-cost {self.cfg.exit_cost_bps:.0f}bps "
                        f"({self.cfg.exit_confirm_polls}× confirmed)",
                        funding_rate=funding,
                        basis_bps=basis,
                    )
                # Severe enough to warrant exit, but not yet confirmed N times.
                return CarryAction(
                    "none",
                    f"EV exit pending ({self._exit_signals}/"
                    f"{self.cfg.exit_confirm_polls}): hold-loss "
                    f"{projected_loss_bps:.0f}bps",
                    funding_rate=funding,
                    basis_bps=basis,
                )
            # Mild negative funding — not worth paying the close cost. Hold on.
            self._exit_signals = 0
        else:
            # Funding positive/neutral — reset the exit confirmation counter.
            self._exit_signals = 0
        # 2b) Max hold time — don't hold a position forever even if funding
        #     is marginally positive. Closes after max_hold_hours (0 = off).
        if self.cfg.max_hold_hours > 0 and self._entry_time is not None:
            held_h = (time.time() - self._entry_time) / 3600.0
            if held_h >= self.cfg.max_hold_hours:
                self.state = CarryState.FLAT
                self.position_qty = 0.0
                return CarryAction(
                    "close",
                    f"max hold {held_h:.1f}h ≥ {self.cfg.max_hold_hours:.0f}h",
                    funding_rate=funding,
                    basis_bps=basis,
                )
        # 3) Hedge drift — keep perp and spot notionals aligned.
        if abs(basis - self.entry_basis_bps) > self.cfg.rebalance_drift_bps:
            return CarryAction(
                "rebalance",
                f"drift {abs(basis - self.entry_basis_bps):.0f}bps",
                funding_rate=funding,
                basis_bps=basis,
            )
        # 4) Hold and collect.
        return CarryAction("none", "holding", funding_rate=funding, basis_bps=basis)

    def _decide_flat(self, funding: float, basis: float, price: float) -> CarryAction:
        if funding < self.cfg.min_funding_to_open:
            return CarryAction(
                "none", f"funding {funding*100:.4f}% < {self.cfg.min_funding_to_open*100:.4f}%",
                funding_rate=funding, basis_bps=basis,
            )
        confidence = self._entry_confidence(funding, basis)
        qty = self._position_size(price, confidence)
        if qty <= 0:
            return CarryAction("none", "no equity / qty=0", funding_rate=funding, basis_bps=basis)
        self.state = CarryState.HEDGED
        self.position_qty = qty
        self.entry_basis_bps = basis
        self._entry_time = time.time()
        return CarryAction(
            "open",
            f"funding {funding*100:.4f}% ≥ {self.cfg.min_funding_to_open*100:.4f}% "
            f"(conf {confidence:.0%})",
            perp_side="Sell",  # short perpetual
            spot_side="Buy",  # long spot hedge
            qty=qty,
            funding_rate=funding,
            basis_bps=basis,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Execution (side effects)
    # ------------------------------------------------------------------
    def execute(self, act: CarryAction) -> dict | None:
        """Turn a CarryAction into exchange orders. Returns the open result."""
        result: dict | None = None
        if act.action == "open":
            result = self._open(act)
        elif act.action == "close":
            result = self._close(act)
        elif act.action == "rebalance":
            result = self._rebalance(act)
        if result is not None:
            self._log_trade(act)
        return result

    def _log_trade(self, act: CarryAction) -> None:
        """Append a row to the persistent CSV trade log (if enabled)."""
        path = self.cfg.trade_log
        if not path:
            return
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            write_header = not p.exists()
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "action": act.action,
                "symbol": self.cfg.symbol,
                "side": act.perp_side or "",
                "qty": act.qty,
                "funding_rate": act.funding_rate,
                "basis_bps": act.basis_bps,
                "perp_price": act.perp_price,
                "spot_price": act.spot_price,
                "confidence": act.confidence,
                "reason": act.reason,
            }
            with open(p, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
                if write_header:
                    w.writeheader()
                w.writerow(row)
        except OSError as exc:
            log.warning("trade_log_write_failed", error=str(exc))

    def _open(self, act: CarryAction) -> dict:
        log.info(
            "carry_open", reason=act.reason, qty=act.qty,
            funding=act.funding_rate, basis_bps=act.basis_bps,
        )
        # Short the perpetual (market, reduce-only off — opening).
        perp = self.exchange.place_order({
            "symbol": self.cfg.symbol, "side": act.perp_side,
            "orderType": "Market", "qty": str(act.qty),
        })
        # Long the spot hedge.  Bybit V5 spot Market BUY takes qty in QUOTE
        # currency (USDT), not base (BTC) — so we convert qty_btc → USDT
        # notional.  (Market SELL still uses base-currency qty; see _close.)
        # CRITICAL: if the spot leg fails we must ROLL BACK the perp short
        # immediately — an unhedged short has unlimited loss risk (squeeze).
        price = act.spot_price or act.perp_price
        spot_qty_usdt = round(act.qty * price, 2) if price > 0 else 0.0
        try:
            spot = self.exchange.place_spot_order({
                "symbol": self.cfg.symbol, "side": act.spot_side,
                "orderType": "Market", "qty": str(spot_qty_usdt),
            })
        except Exception as exc:
            log.error("carry_open_spot_failed", error=str(exc))
            self._emergency_close_perp(act.qty)
            self.state = CarryState.FLAT
            self.position_qty = 0.0
            raise
        # Set exchange-side backstop stop-loss (protects while bot is offline).
        self._set_exchange_stop_loss(act.perp_price or price)
        return {"perp": perp, "spot": spot}

    def _emergency_close_perp(self, qty: float) -> None:
        """Best-effort close of an unhedged perp short after a spot failure."""
        try:
            self.exchange.place_order({
                "symbol": self.cfg.symbol, "side": "Buy",
                "orderType": "Market", "qty": str(qty), "reduceOnly": True,
            })
            log.warning("carry_open_rolled_back", qty=qty)
        except Exception as rb_exc:
            log.error("carry_open_rollback_failed", qty=qty, error=str(rb_exc))

    def _close(self, act: CarryAction) -> dict:
        qty = act.qty or self.position_qty
        log.info("carry_close", qty=qty, reason=act.reason)
        # Buy back the perp short (reduceOnly ensures it only closes).
        perp = self.exchange.place_order({
            "symbol": self.cfg.symbol, "side": "Buy",
            "orderType": "Market", "qty": str(qty), "reduceOnly": True,
        })
        # Sell the spot long.  CRITICAL: the actual spot BTC balance is
        # slightly LESS than the perp qty because the spot taker fee (~0.1%)
        # was deducted from the received BTC at open time.  Selling the full
        # perp qty would FAIL (insufficient balance).  Read the real balance.
        _, spot_size = self._leg_sizes()
        sell_qty = spot_size if spot_size > 0 else qty
        spot: dict | None = None
        try:
            spot = self.exchange.place_spot_order({
                "symbol": self.cfg.symbol, "side": "Sell",
                "orderType": "Market", "qty": str(round(sell_qty, 8)),
            })
        except Exception as exc:
            # Perp is closed; leftover spot is not dangerous (no liquidation
            # risk on a spot long), but log so the operator can clean up.
            log.error("carry_close_spot_failed", error=str(exc), sell_qty=sell_qty)
        # Clear the exchange-side stop-loss (perp position is gone).
        self._clear_exchange_stop_loss()
        self.position_qty = 0.0
        self.entry_basis_bps = 0.0
        self._entry_time = None
        return {"perp": perp, "spot": spot}

    # ------------------------------------------------------------------
    # Exchange-side backstop stop-loss
    # ------------------------------------------------------------------
    def _set_exchange_stop_loss(self, entry_price: float) -> None:
        """Set a Bybit trading-stop SL on the perp short.

        This order lives on Bybit's servers and triggers **even when the bot
        is offline** — the last line of defence against a short squeeze.
        For a short, the SL price is *above* entry (price rising = loss).
        """
        if self.cfg.stop_loss_pct <= 0 or entry_price <= 0:
            return
        sl_price = entry_price * (1 + self.cfg.stop_loss_pct / 100.0)
        try:
            self.exchange.set_trading_stop(
                self.cfg.symbol, stop_loss=str(round(sl_price, 1))
            )
            log.info("carry_stop_loss_set", sl_price=sl_price, pct=self.cfg.stop_loss_pct)
        except Exception as exc:
            log.warning("carry_stop_loss_set_failed", error=str(exc))

    def _clear_exchange_stop_loss(self) -> None:
        """Remove the exchange-side stop-loss (called on close)."""
        try:
            self.exchange.set_trading_stop(self.cfg.symbol, stop_loss="0")
            log.info("carry_stop_loss_cleared")
        except Exception as exc:
            log.warning("carry_stop_loss_clear_failed", error=str(exc))

    def _check_liq_proximity(self) -> None:
        """Warn if mark price is within ``liq_warning_pct`` of liquidation.

        Best-effort and throttled (every ~12 polls ≈ 1 min at 5 s interval)
        to avoid an extra API call on every single poll.  Side-effect only —
        logs a warning but does not change the position or decision.
        """
        if self.cfg.liq_warning_pct <= 0:
            return
        if self._poll_count % 12 != 0:
            return
        try:
            for p in self.exchange.get_positions(self.cfg.symbol):
                if p.get("symbol") != self.cfg.symbol:
                    continue
                liq = _safe_float(p.get("liqPrice"))
                mark = _safe_float(p.get("markPrice"))
                if liq > 0 and mark > 0:
                    dist_pct = abs(mark - liq) / mark * 100.0
                    if dist_pct < self.cfg.liq_warning_pct:
                        log.warning(
                            "carry_liq_proximity",
                            liq_price=liq, mark_price=mark,
                            dist_pct=round(dist_pct, 1),
                            warning_pct=self.cfg.liq_warning_pct,
                        )
                    break
        except Exception:
            pass  # best-effort; never crash the decision loop

    def _leg_sizes(self) -> tuple[float, float]:
        """Actual ``(perp_short_size_btc, spot_long_size_btc)`` from the exchange.

        Best-effort: any leg that cannot be read returns ``0.0`` so the caller
        can decide whether rebalancing is safe (we never want to "correct" a
        mismatch we can't actually measure).
        """
        perp_size = 0.0
        try:
            for p in self.exchange.get_positions(self.cfg.symbol):
                if p.get("symbol") == self.cfg.symbol:
                    perp_size = abs(_safe_float(p.get("size")))
                    break
        except Exception as exc:  # network / parse — don't let it crash the loop
            log.warning("rebalance_read_perp_failed", error=str(exc))
        spot_size = 0.0
        try:
            res = self.exchange.get_wallet_balance(self.cfg.base_coin)
            for c in res["list"][0]["coin"]:
                if c.get("coin") == self.cfg.base_coin:
                    spot_size = _safe_float(c.get("walletBalance"))
                    break
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log.warning("rebalance_read_spot_failed", error=str(exc))
        return perp_size, spot_size

    def _rebalance(self, act: CarryAction) -> dict:
        """Re-align the perp and spot legs so the position stays delta-neutral.

        Reads the *actual* leg sizes from the exchange; if they diverge by more
        than ``rebalance_min_btc``, places a corrective order on the **spot**
        leg (cheaper to adjust, and leaves the funding-collecting perp alone):

        * net short (perp > spot) → BUY more spot (qty in **USDT**)
        * net long  (spot > perp) → SELL spot     (qty in **BTC**)

        The basis baseline is always reset afterwards, so a no-op rebalance
        (mismatch below threshold) doesn't re-trigger on every poll.
        """
        log.info("carry_rebalance", reason=act.reason, basis_bps=act.basis_bps)
        perp_size, spot_size = self._leg_sizes()
        delta = perp_size - spot_size  # >0 net short, <0 net long
        price = act.spot_price or act.perp_price
        result: dict = {
            "rebalanced": False, "perp_size": perp_size,
            "spot_size": spot_size, "delta_btc": delta,
        }
        if abs(delta) < self.cfg.rebalance_min_btc or price <= 0:
            log.info(
                "carry_rebalance_skipped", delta_btc=delta,
                min_btc=self.cfg.rebalance_min_btc,
            )
            self.entry_basis_bps = act.basis_bps
            return result
        try:
            if delta > 0:
                # Net short → top up the spot long. Spot Market BUY qty is USDT.
                qty_usdt = round(delta * price, 2)
                self.exchange.place_spot_order({
                    "symbol": self.cfg.symbol, "side": "Buy",
                    "orderType": "Market", "qty": str(qty_usdt),
                })
                log.info("carry_rebalance_buy_spot", qty_usdt=qty_usdt, delta_btc=delta)
            else:
                # Net long → trim the spot long. Spot Market SELL qty is BTC.
                qty_btc = round(abs(delta), 8)
                self.exchange.place_spot_order({
                    "symbol": self.cfg.symbol, "side": "Sell",
                    "orderType": "Market", "qty": str(qty_btc),
                })
                log.info("carry_rebalance_sell_spot", qty_btc=qty_btc, delta_btc=delta)
            result["rebalanced"] = True
        except Exception as exc:
            log.error("carry_rebalance_failed", error=str(exc))
        self.entry_basis_bps = act.basis_bps
        return result

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    def run_once(self) -> CarryAction:
        """Decide + execute in one call (the live loop body)."""
        act = self.decide()
        self.execute(act)
        return act


def _safe_float(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0
