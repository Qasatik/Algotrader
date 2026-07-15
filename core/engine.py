"""Trading engine: orchestrates data -> inference -> strategy -> risk -> orders.

The engine is the central object the Telegram admin bot controls. It exposes
``start()``, ``stop()``, and ``kill_switch()`` plus read-only status accessors
so an operator can monitor and intervene remotely.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from bot.alerts import notify
from config.settings import get_settings
from core.data_feed import Candle, MarketDataFeed
from core.exchange import BybitExchange
from core.order_manager import OrderManager
from core.risk_manager import RiskManager
from core.strategy import MlOrderbookStrategy, Signal
from ml.inference import InferenceEngine
from utils.logger import get_logger

log = get_logger("engine")


class EngineState(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"        # listening to data but not placing orders
    KILLED = "killed"        # hard stop, flattened intent, requires manual restart


@dataclass
class EngineStats:
    started_at: datetime | None = None
    signals_generated: int = 0
    orders_placed: int = 0
    orders_filled: int = 0
    orders_failed: int = 0
    last_signal: dict = field(default_factory=dict)


class TradingEngine:
    """The runnable trading loop."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.state = EngineState.STOPPED
        self.stats = EngineStats()

        # Wire components together
        self.exchange = BybitExchange()
        self.feed = MarketDataFeed()
        self.inference = InferenceEngine()
        self.strategy = MlOrderbookStrategy(self.inference, self.feed)
        self.risk = RiskManager(self.exchange)
        self.orders = OrderManager(self.exchange, self.feed)

        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._log = log.bind(symbol=self.settings.trading_symbol)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Start the engine: configure leverage, seed data, begin loop."""
        if self.state == EngineState.RUNNING:
            self._log.warning("already_running")
            return
        if self.state == EngineState.KILLED:
            self._log.error("engine_killed_requires_restart")
            return

        # Configure leverage once
        try:
            await asyncio.to_thread(
                self.exchange.set_leverage,
                self.settings.trading_symbol,
                self.settings.leverage,
            )
        except Exception as exc:
            self._log.warning("leverage_setup_skipped", error=str(exc))

        # Seed candle history then start websocket
        await asyncio.to_thread(self.feed.seed_from_rest, self.exchange)
        self.feed.on_closed_candle(self._on_candle)
        await self.feed.start()

        self.state = EngineState.RUNNING
        self.stats.started_at = datetime.now(timezone.utc)
        self._stop_event.clear()
        self._log.info("engine_started", mode="paper" if self.settings.is_paper_mode else "LIVE")

    async def stop(self) -> None:
        """Graceful stop: disconnect websocket, keep positions open."""
        self.state = EngineState.STOPPED
        self._stop_event.set()
        await self.feed.stop()
        self._log.info("engine_stopped")

    async def pause(self) -> None:
        """Pause order placement but keep streaming data."""
        if self.state == EngineState.RUNNING:
            self.state = EngineState.PAUSED
            self._log.info("engine_paused")

    async def resume(self) -> None:
        if self.state == EngineState.PAUSED:
            self.state = EngineState.RUNNING
            self._log.info("engine_resumed")

    async def kill_switch(self) -> str:
        """Emergency stop: stop engine and flatten the current position.

        Returns a human-readable status string for the Telegram bot.
        """
        self.state = EngineState.KILLED
        self._stop_event.set()
        await self.feed.stop()
        result = await self._flatten_position()
        self._log.error("KILL_SWITCH_ACTIVATED", flatten_result=result)
        await notify(f"🛑 KILL SWITCH activated. {result}")
        return f"🛑 KILL SWITCH activated. {result}"

    def _reset_killed(self) -> None:
        """Allow restart after a kill (manual confirmation)."""
        if self.state == EngineState.KILLED:
            self.state = EngineState.STOPPED

    # ------------------------------------------------------------------
    # Core signal handler (called on each closed candle)
    # ------------------------------------------------------------------
    async def _on_candle(self, candle: Candle) -> None:
        if self.state != EngineState.RUNNING:
            return
        try:
            signal: Signal | None = await self.strategy.on_candle(candle)
            if signal is None:
                return

            self.stats.signals_generated += 1
            self.stats.last_signal = {
                "side": signal.side.value,
                "price": signal.entry_price,
                "confidence": round(signal.confidence, 3),
                "ts": datetime.now(timezone.utc).isoformat(),
            }

            trade = self.risk.approve(signal)
            if trade is None:
                # Alert if a circuit breaker (not just sizing) halted us.
                if self.risk.halted_reason:
                    await notify(
                        f"⛔ Trading halted: {self.risk.halted_reason}"
                    )
                return

            self.stats.orders_placed += 1
            res = await self.orders.execute(trade)
            if res:
                self.stats.orders_filled += 1
                await notify(
                    f"✅ Order filled: {trade.side.value} {trade.qty} "
                    f"{self.settings.trading_symbol} @ ~{trade.entry_price:.2f}"
                )
            else:
                self.stats.orders_failed += 1
                await notify(
                    f"⚠️ Order FAILED: {trade.side.value} {trade.qty} "
                    f"{self.settings.trading_symbol}"
                )
        except Exception as exc:
            self._log.error("on_candle_error", error=str(exc))
            await notify(f"❌ Engine error: {exc}")

    # ------------------------------------------------------------------
    # Read-only status (consumed by Telegram admin bot)
    # ------------------------------------------------------------------
    async def status(self) -> dict:
        positions = await asyncio.to_thread(
            self.exchange.get_positions, self.settings.trading_symbol
        )
        equity = await asyncio.to_thread(self._safe_equity)
        return {
            "state": self.state.value,
            "mode": "paper" if self.settings.is_paper_mode else "LIVE",
            "symbol": self.settings.trading_symbol,
            "equity_usdt": round(equity, 2),
            "leverage": self.settings.leverage,
            "positions": positions,
            "mid_price": self.feed.book.mid_price,
            "candles_buffered": len(self.feed.candles),
            "stats": {
                "signals": self.stats.signals_generated,
                "orders_placed": self.stats.orders_placed,
                "orders_filled": self.stats.orders_filled,
                "orders_failed": self.stats.orders_failed,
                "uptime_since": self.stats.started_at.isoformat()
                if self.stats.started_at
                else None,
            },
        }

    def _safe_equity(self) -> float:
        try:
            res = self.exchange.get_wallet_balance("USDT")
            coins = res.get("list", [{}])[0].get("coin", [])
            for c in coins:
                if c.get("coin") == "USDT":
                    return float(c.get("walletBalance", 0))
        except Exception:
            return 0.0
        return 0.0

    async def _flatten_position(self) -> str:
        """Close any open position with a reducing market order."""
        try:
            positions = await asyncio.to_thread(
                self.exchange.get_positions, self.settings.trading_symbol
            )
            for p in positions:
                size = float(p.get("size", 0))
                if size == 0:
                    continue
                side = "Sell" if size > 0 else "Buy"
                await asyncio.to_thread(
                    self.exchange.place_order,
                    {
                        "symbol": self.settings.trading_symbol,
                        "side": side,
                        "orderType": "Market",
                        "qty": str(abs(size)),
                        "reduceOnly": True,
                        "timeInForce": "IOC",
                    },
                )
                return f"Flattened position size={size}."
            return "No open position to flatten."
        except Exception as exc:
            return f"Flatten failed: {exc}"
