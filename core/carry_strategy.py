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

from dataclasses import dataclass
from enum import Enum

from core.exchange import BybitExchange
from utils.logger import get_logger

log = get_logger("carry_strategy")


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
    close_funding: float = -0.0001  # flatten if funding goes negative (< -0.01%)
    basis_guard_bps: float = 50.0  # flatten if perp premium > 50 bps (0.5%)
    rebalance_drift_bps: float = 20.0  # rebalance hedge if basis drifts > 20 bps
    qty_step: float = 0.001  # BTC lot step (round qty down to this)
    paper_equity: float | None = None  # if set, override wallet balance (dry-run)


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

    def _position_size(self, price: float) -> float:
        """Conservative notional → quantity.

        notional = equity_fraction × equity. Total capital deployed across both
        legs ≈ notional × (1 + 1/leverage); with defaults (0.5, 2×) that is
        0.75 × equity, leaving a 25% buffer. Quantity is rounded down to the
        exchange lot step.
        """
        equity = self._equity_usdt()
        notional = equity * self.cfg.equity_fraction
        raw_qty = notional / price if price > 0 else 0.0
        step = self.cfg.qty_step
        return max((raw_qty // step) * step, 0.0)

    # ------------------------------------------------------------------
    # Decision logic (pure / testable)
    # ------------------------------------------------------------------
    def decide(self) -> CarryAction:
        """Poll funding + basis and return the next action.

        Updates ``self.state`` / ``self.position_qty`` as a side effect so the
        strategy is stateful across calls.
        """
        fr = self.exchange.get_funding_rate(self.cfg.symbol)
        funding = _safe_float(fr.get("fundingRate"))
        perp_price = _safe_float(fr.get("markPrice")) or _safe_float(fr.get("lastPrice"))
        spot_price = self.exchange.get_spot_price(self.cfg.symbol) or perp_price or 0.0
        basis = _basis_bps(perp_price, spot_price)

        if self.state == CarryState.HEDGED:
            return self._decide_hedged(funding, basis)
        return self._decide_flat(funding, basis, perp_price or spot_price)

    def _decide_hedged(self, funding: float, basis: float) -> CarryAction:
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
        # 2) Funding turned negative — carry no longer pays, exit.
        if funding < self.cfg.close_funding:
            self.state = CarryState.FLAT
            self.position_qty = 0.0
            return CarryAction(
                "close",
                f"funding {funding*100:.4f}% < {self.cfg.close_funding*100:.4f}%",
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
        qty = self._position_size(price)
        if qty <= 0:
            return CarryAction("none", "no equity / qty=0", funding_rate=funding, basis_bps=basis)
        self.state = CarryState.HEDGED
        self.position_qty = qty
        self.entry_basis_bps = basis
        return CarryAction(
            "open",
            f"funding {funding*100:.4f}% ≥ {self.cfg.min_funding_to_open*100:.4f}%",
            perp_side="Sell",  # short perpetual
            spot_side="Buy",  # long spot hedge
            qty=qty,
            funding_rate=funding,
            basis_bps=basis,
        )

    # ------------------------------------------------------------------
    # Execution (side effects)
    # ------------------------------------------------------------------
    def execute(self, act: CarryAction) -> dict | None:
        """Turn a CarryAction into exchange orders. Returns the open result."""
        if act.action == "open":
            return self._open(act)
        if act.action == "close":
            return self._close()
        if act.action == "rebalance":
            return self._rebalance(act)
        return None

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
        # Long the spot hedge at notional ≈ qty × price (market).
        spot = self.exchange.place_spot_order({
            "symbol": self.cfg.symbol, "side": act.spot_side,
            "orderType": "Market", "qty": str(act.qty),
        })
        return {"perp": perp, "spot": spot}

    def _close(self) -> dict:
        qty = self.position_qty
        log.info("carry_close", qty=qty)
        # Buy back the perp short + sell the spot long.
        perp = self.exchange.place_order({
            "symbol": self.cfg.symbol, "side": "Buy",
            "orderType": "Market", "qty": str(qty), "reduceOnly": True,
        })
        spot = self.exchange.place_spot_order({
            "symbol": self.cfg.symbol, "side": "Sell",
            "orderType": "Market", "qty": str(qty),
        })
        self.position_qty = 0.0
        return {"perp": perp, "spot": spot}

    def _rebalance(self, act: CarryAction) -> dict:
        log.info("carry_rebalance", reason=act.reason, basis_bps=act.basis_bps)
        # In production: compute the delta between perp and spot notionals and
        # trim/augment the smaller leg. Stubbed here — returns current state.
        return {"rebalanced": True, "basis_bps": act.basis_bps}

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
