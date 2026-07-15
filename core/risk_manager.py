"""Risk management: position sizing, exposure limits, and kill-switch.

The RiskManager is the single gatekeeper that decides whether a proposed
trade is allowed and, if so, how large it should be. No order may be placed
without passing through ``approve()``.

Includes circuit breakers (R1/R2):
  * daily drawdown limit  -> pause trading for the rest of the day,
  * consecutive-loss limit -> pause for a cooldown window.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from config.settings import get_settings
from core.exchange import BybitExchange
from core.strategy import Side, Signal
from utils.logger import get_logger

log = get_logger("risk")


@dataclass
class ApprovedTrade:
    side: Side
    qty: float           # base-asset quantity (e.g. BTC)
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_amount: float   # quote currency (USDT) at stake if SL hit


class RiskManager:
    """Computes safe position sizes from account equity and SL distance."""

    def __init__(self, exchange: BybitExchange) -> None:
        self.exchange = exchange
        s = get_settings()
        self.risk_per_trade = s.risk_per_trade
        self.max_positions = s.max_open_positions
        self.leverage = s.leverage
        self.symbol = s.trading_symbol
        # Circuit breaker config & state (R1/R2)
        self.max_daily_drawdown = s.max_daily_drawdown
        self.max_consecutive_losses = s.max_consecutive_losses
        self.cooldown_minutes = s.cooldown_minutes
        self._day_start_equity: float | None = None
        self._current_day: str = ""
        self._consecutive_losses = 0
        self._cooldown_until: float = 0.0
        self._halted_reason: str | None = None
        self._log = log.bind(symbol=self.symbol)

    # ------------------------------------------------------------------
    # Circuit breakers (R1/R2)
    # ------------------------------------------------------------------
    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _maybe_roll_day(self, equity: float) -> None:
        """Reset the daily equity baseline at each new UTC day."""
        today = self._today()
        if today != self._current_day:
            self._current_day = today
            self._day_start_equity = equity
            self._halted_reason = None  # new day clears a daily-DD halt

    def _check_breakers(self, equity: float) -> str | None:
        """Return a halt reason if a breaker is tripped, else None."""
        # Cooldown window after consecutive losses
        if time.time() < self._cooldown_until:
            mins_left = (self._cooldown_until - time.time()) / 60.0
            return f"cooldown ({mins_left:.1f}m left)"

        # Daily drawdown
        if self._day_start_equity and self._day_start_equity > 0:
            dd = (self._day_start_equity - equity) / self._day_start_equity
            if dd >= self.max_daily_drawdown:
                return (f"daily drawdown {dd:.1%} >= limit "
                        f"{self.max_daily_drawdown:.1%}")
        return None

    def register_closed_trade(self, pnl_usdt: float) -> None:
        """Feed realized PnL of each closed trade to update breakers.

        Call this from the engine when a position is closed (SL/TP/manual).
        """
        if pnl_usdt < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.max_consecutive_losses:
                self._cooldown_until = time.time() + self.cooldown_minutes * 60
                self._halted_reason = (
                    f"{self._consecutive_losses} consecutive losses -> "
                    f"cooldown {self.cooldown_minutes}m"
                )
                self._log.warning("breaker_consecutive_losses",
                                  losses=self._consecutive_losses)
        else:
            self._consecutive_losses = 0

    @property
    def halted_reason(self) -> str | None:
        return self._halted_reason

    # ------------------------------------------------------------------
    # Account state
    # ------------------------------------------------------------------
    def _equity_usdt(self) -> float:
        """Available USDT equity in the unified account."""
        try:
            res = self.exchange.get_wallet_balance("USDT")
            accounts = res.get("list", [])
            if not accounts:
                return 0.0
            # unified account -> coin list
            coins = accounts[0].get("coin", [])
            for coin in coins:
                if coin.get("coin") == "USDT":
                    return float(coin.get("walletBalance", 0))
        except Exception as exc:
            self._log.error("equity_lookup_failed", error=str(exc))
        return 0.0

    def _open_position_count(self) -> int:
        try:
            positions = self.exchange.get_positions(symbol=self.symbol)
            return sum(
                1 for p in positions if float(p.get("size", 0)) != 0.0
            )
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Approval gate
    # ------------------------------------------------------------------
    def approve(self, signal: Signal) -> ApprovedTrade | None:
        """Return a sized trade if the signal passes all risk checks."""
        if signal.side == Side.HOLD:
            return None

        equity = self._equity_usdt()
        if equity <= 0:
            self._log.warning("no_equity_skip")
            return None

        # Circuit breakers (R1/R2): roll the day, then check limits.
        self._maybe_roll_day(equity)
        halt = self._check_breakers(equity)
        if halt:
            self._halted_reason = halt
            self._log.warning("breaker_halted_trading", reason=halt)
            return None

        if self._open_position_count() >= self.max_positions:
            self._log.info("max_positions_reached", count=self.max_positions)
            return None

        # Risk amount = fraction of equity we accept to lose if SL is hit.
        risk_amount = equity * self.risk_per_trade

        # SL distance in price terms
        sl_distance = abs(signal.entry_price - signal.stop_loss)
        if sl_distance <= 0:
            self._log.warning("invalid_sl_distance")
            return None

        # qty so that (qty * sl_distance) == risk_amount
        qty = risk_amount / sl_distance

        # Cap by leverage-based notional so we never exceed (equity * lev).
        max_notional = equity * self.leverage
        notional = qty * signal.entry_price
        if notional > max_notional:
            qty = max_notional / signal.entry_price
            risk_amount = qty * sl_distance
            self._log.info(
                "size_capped_by_leverage", notional=notional, max_notional=max_notional
            )

        if qty <= 0:
            return None

        trade = ApprovedTrade(
            side=signal.side,
            qty=qty,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            risk_amount=risk_amount,
        )
        self._log.info(
            "trade_approved",
            side=trade.side.value,
            qty=trade.qty,
            entry=trade.entry_price,
            sl=trade.stop_loss,
            tp=trade.take_profit,
            risk_usdt=round(trade.risk_amount, 2),
            equity=round(equity, 2),
        )
        return trade
