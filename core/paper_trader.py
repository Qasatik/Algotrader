"""Paper-trading simulation engine for dry-run mode (P3-16).

Wraps the LIVE decision logic with a virtual position tracker: applies
funding payments, trading fees, and price-based P&L so you can see how the
strategy WOULD perform on real-time data without risking capital.

Unlike the vectorized backtest (``backtest/``), this runs the ACTUAL
:class:`CarryStrategy` state machine poll-by-poll against live market data —
so basis guards, EV-gated exits, conviction sizing etc. are all exercised
exactly as they would be in production.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from core.carry_strategy import CarryAction
from utils.logger import get_logger

log = get_logger("paper")

# Bybit pays funding every 8h at 00:00, 08:00, 16:00 UTC.
FUNDING_INTERVAL_S = 8 * 3600


@dataclass
class PaperPosition:
    """Virtual delta-neutral carry position (short perp + long spot)."""

    qty_btc: float
    entry_perp_price: float
    entry_spot_price: float
    entry_funding: float
    entry_time: float  # epoch seconds
    funding_collected: float = 0.0
    last_funding_ts: float = 0.0


@dataclass
class PaperStats:
    """Snapshot of the paper-trading account for display / logging."""

    starting_equity: float
    equity: float
    cash: float
    unrealised_pnl: float
    realised_pnl: float
    total_funding: float
    total_fees: float
    trade_count: int
    position_qty: float
    entry_price: float

    @property
    def total_pnl(self) -> float:
        """Realised + unrealised profit/loss (USDT)."""
        return self.realised_pnl + self.unrealised_pnl

    @property
    def total_return_pct(self) -> float:
        """Total return as % of starting equity."""
        if self.starting_equity <= 0:
            return 0.0
        return self.total_pnl / self.starting_equity * 100.0


class PaperTrader:
    """Simulated execution engine for dry-run mode (P3-16).

    Tracks a virtual delta-neutral carry position: applies funding payments
    every 8h, deducts trading fees on open/close, and computes unrealised +
    realised P&L from real market prices.

    Usage in a dry-run runner::

        paper = PaperTrader(starting_equity=args.paper_equity)
        ...
        act = strategy.decide()
        paper.apply(act, funding, perp_price, spot_price)
        if poll % heartbeat == 0:
            print(paper.format_stats(perp_price, spot_price))
    """

    def __init__(
        self,
        starting_equity: float = 10000.0,
        perp_taker_fee: float = 0.00055,  # 0.055% Bybit linear taker
        spot_taker_fee: float = 0.001,  # 0.1% Bybit spot taker
    ) -> None:
        self.starting_equity = starting_equity
        self.cash = starting_equity
        self.perp_taker_fee = perp_taker_fee
        self.spot_taker_fee = spot_taker_fee
        self.position: PaperPosition | None = None
        self.total_fees = 0.0
        self.total_funding = 0.0  # cumulative from CLOSED positions
        self.trade_count = 0

    @property
    def is_open(self) -> bool:
        """True if a virtual position is currently held."""
        return self.position is not None

    # ------------------------------------------------------------------
    # Core lifecycle
    # ------------------------------------------------------------------
    def apply(
        self,
        action: CarryAction,
        funding_rate: float,
        perp_price: float,
        spot_price: float,
        now: float | None = None,
    ) -> None:
        """Apply a :class:`CarryAction` to the virtual account.

        Always accrues funding first (in case wall-clock time has crossed an
        8h boundary), then processes the action.
        """
        now = now if now is not None else time.time()
        self._accrue_funding(funding_rate, perp_price, now)

        if action.action == "open" and self.position is None:
            self._open(action, perp_price, spot_price, now)
        elif action.action == "close" and self.position is not None:
            self._close(perp_price, spot_price, now)
        elif action.action == "rebalance":
            # Rebalancing adjusts the spot leg to match the perp; for a
            # delta-neutral position the net P&L is unaffected (both legs
            # move together). We log it but don't change the virtual P&L.
            log.info("paper_rebalance", reason=action.reason)

    def _open(
        self, action: CarryAction, perp_price: float, spot_price: float, now: float
    ) -> None:
        qty = action.qty
        # Open fees: perp taker + spot taker on each leg's notional.
        fee = qty * (perp_price * self.perp_taker_fee + spot_price * self.spot_taker_fee)
        self.cash -= fee
        self.total_fees += fee
        self.position = PaperPosition(
            qty_btc=qty,
            entry_perp_price=perp_price,
            entry_spot_price=spot_price,
            entry_funding=action.funding_rate or 0.0,
            entry_time=now,
            last_funding_ts=self._floor_to_funding_ts(now),
        )
        self.trade_count += 1
        log.info(
            "paper_open",
            qty=qty,
            perp_price=perp_price,
            funding_rate=action.funding_rate,
            fee=round(fee, 4),
            cash=round(self.cash, 2),
        )

    def _close(self, perp_price: float, spot_price: float, now: float) -> None:
        pos = self.position
        assert pos is not None
        # Close fees.
        fee = pos.qty_btc * (
            perp_price * self.perp_taker_fee + spot_price * self.spot_taker_fee
        )
        # Delta-neutral P&L: perp short gains when price falls, spot long
        # gains when price rises. For a perfectly hedged position these
        # cancel; the residual is the basis difference at open vs close.
        perp_pnl = (pos.entry_perp_price - perp_price) * pos.qty_btc
        spot_pnl = (spot_price - pos.entry_spot_price) * pos.qty_btc
        net = perp_pnl + spot_pnl + pos.funding_collected - fee
        self.cash += perp_pnl + spot_pnl + pos.funding_collected - fee
        self.total_fees += fee
        self.total_funding += pos.funding_collected
        log.info(
            "paper_close",
            perp_pnl=round(perp_pnl, 4),
            spot_pnl=round(spot_pnl, 4),
            funding=round(pos.funding_collected, 4),
            fee=round(fee, 4),
            net_pnl=round(net, 4),
            cash=round(self.cash, 2),
        )
        self.position = None

    # ------------------------------------------------------------------
    # Funding accrual
    # ------------------------------------------------------------------
    def _accrue_funding(
        self, funding_rate: float, perp_price: float, now: float
    ) -> None:
        """Apply funding payments for each 8h boundary crossed since last.

        Positive funding rate → shorts RECEIVE payment (the carry income).
        Uses the *current* funding rate as an approximation for each missed
        boundary (in live trading the rate is set 8h in advance).
        """
        if self.position is None:
            return
        pos = self.position
        while now >= pos.last_funding_ts + FUNDING_INTERVAL_S:
            notional = pos.qty_btc * perp_price
            payment = notional * funding_rate
            pos.funding_collected += payment
            pos.last_funding_ts += FUNDING_INTERVAL_S
            log.debug(
                "paper_funding",
                amount=round(payment, 6),
                rate=funding_rate,
                cumulative=round(pos.funding_collected, 6),
            )

    @staticmethod
    def _floor_to_funding_ts(ts: float) -> float:
        """Floor an epoch to the most recent 8h funding boundary (UTC)."""
        return ts - (ts % FUNDING_INTERVAL_S)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def unrealised_pnl(self, perp_price: float, spot_price: float) -> float:
        """Current unrealised P&L (price move + accrued funding, pre-close-fee)."""
        if self.position is None:
            return 0.0
        pos = self.position
        perp_pnl = (pos.entry_perp_price - perp_price) * pos.qty_btc
        spot_pnl = (spot_price - pos.entry_spot_price) * pos.qty_btc
        return perp_pnl + spot_pnl + pos.funding_collected

    def equity(self, perp_price: float, spot_price: float) -> float:
        """Total account equity (cash + unrealised)."""
        return self.cash + self.unrealised_pnl(perp_price, spot_price)

    def stats(
        self, perp_price: float = 0.0, spot_price: float = 0.0
    ) -> PaperStats:
        """Return a snapshot of the paper account."""
        unrl = self.unrealised_pnl(perp_price, spot_price) if self.position else 0.0
        eq = self.cash + unrl
        return PaperStats(
            starting_equity=self.starting_equity,
            equity=eq,
            cash=self.cash,
            unrealised_pnl=unrl,
            realised_pnl=self.cash - self.starting_equity,
            total_funding=self.total_funding
            + (self.position.funding_collected if self.position else 0.0),
            total_fees=self.total_fees,
            trade_count=self.trade_count,
            position_qty=self.position.qty_btc if self.position else 0.0,
            entry_price=self.position.entry_perp_price if self.position else 0.0,
        )

    def format_stats(
        self, perp_price: float = 0.0, spot_price: float = 0.0
    ) -> str:
        """Human-readable multi-line report for CLI / logs."""
        s = self.stats(perp_price, spot_price)
        lines = [
            "📊 Paper Trading Report",
            f"  starting equity: ${s.starting_equity:,.2f}",
            f"  current equity:  ${s.equity:,.2f}",
            f"  realised PnL:    ${s.realised_pnl:+,.2f}",
            f"  unrealised PnL:  ${s.unrealised_pnl:+,.2f}",
            f"  total PnL:       ${s.total_pnl:+,.2f} ({s.total_return_pct:+.2f}%)",
            f"  funding earned:  ${s.total_funding:,.4f}",
            f"  total fees:      ${s.total_fees:,.4f}",
            f"  trades:          {s.trade_count}",
        ]
        if s.position_qty > 0:
            lines.append(
                f"  position:        {s.position_qty:.4f} BTC @ ${s.entry_price:,.2f}"
            )
        else:
            lines.append("  position:        flat")
        return "\n".join(lines)
