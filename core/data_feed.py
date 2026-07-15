"""Real-time market data feed via Bybit WebSocket (V5 public channel).

Subscribes to:
  * orderbook.{depth}.{symbol}  -> L2 depth snapshots
  * kline.{interval}.{symbol}   -> candle updates

Maintains an in-memory rolling buffer of recent candles so the ML model
always has a full lookback window ready for inference.
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pybit.unified_trading import WebSocket

from config.settings import get_settings
from utils.logger import get_logger

log = get_logger("datafeed")


@dataclass
class Candle:
    """A single OHLCV candle."""
    start_time: int  # ms epoch
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_row(cls, row: list[str]) -> Candle:
        return cls(
            start_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )


@dataclass
class OrderBook:
    """Latest top-of-book + depth snapshot."""
    bids: list[list[str]] = field(default_factory=list)
    asks: list[list[str]] = field(default_factory=list)
    ts: int = 0

    @property
    def best_bid(self) -> float | None:
        return float(self.bids[0][0]) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return float(self.asks[0][0]) if self.asks else None

    @property
    def mid_price(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return None


# Type alias for the async callback fired on every new closed candle.
CandleCallback = Callable[["Candle"], Awaitable[None]]


class MarketDataFeed:
    """Owns the websocket lifecycle and the candle buffer."""

    def __init__(self, symbol: str | None = None) -> None:
        s = get_settings()
        self.symbol = symbol or s.trading_symbol
        self.depth = s.orderbook_depth
        self.interval = s.kline_interval
        self.seq_len = s.ml_sequence_len

        self.book = OrderBook()
        self.candles: deque[Candle] = deque(maxlen=self.seq_len * 2)
        self._on_candle: CandleCallback | None = None
        self._ws: WebSocket | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def on_closed_candle(self, cb: CandleCallback) -> None:
        """Register an async callback invoked when a candle closes."""
        self._on_candle = cb

    def latest_closes(self, n: int | None = None) -> list[float]:
        """Return the last `n` close prices (default: sequence length)."""
        n = n or self.seq_len
        return [c.close for c in list(self.candles)[-n:]]

    def seed_from_rest(self, exchange) -> None:
        """Backfill the candle buffer from REST history (call before WS start)."""
        rows = exchange.get_klines(self.symbol, self.interval, limit=self.seq_len + 5)
        # Bybit returns newest-first; reverse for chronological order
        for row in reversed(rows):
            self.candles.append(Candle.from_row(row))
        log.info("seeded_candles", symbol=self.symbol, count=len(self.candles))

    async def start(self) -> None:
        """Connect the websocket and subscribe to channels."""
        s = get_settings()
        self._ws = WebSocket(testnet=s.is_paper_mode, channel_type="linear")

        self._ws.orderbook_stream(
            depth=self.depth,
            symbol=self.symbol,
            callback=self._handle_orderbook,
        )
        self._ws.kline_stream(
            interval=int(self.interval),
            symbol=self.symbol,
            callback=self._handle_kline,
        )
        log.info("ws_started", symbol=self.symbol, depth=self.depth, interval=self.interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.exit()
            except Exception:
                pass
        log.info("ws_stopped")

    # ------------------------------------------------------------------
    # WebSocket callbacks (pybit calls these from its own thread)
    # ------------------------------------------------------------------
    def _handle_orderbook(self, message: dict[str, Any]) -> None:
        data = message.get("data")
        if not data:
            return
        self.book.bids = data.get("b", self.book.bids)
        self.book.asks = data.get("a", self.book.asks)
        self.book.ts = message.get("ts", 0)

    def _handle_kline(self, message: dict[str, Any]) -> None:
        data = message.get("data")
        if not data:
            return
        for item in data:
            candle = Candle(
                start_time=int(item["start"]),
                open=float(item["open"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                volume=float(item["volume"]),
            )
            # Only act on a *confirmed* (closed) candle.
            confirmed = item.get("confirm", "0") in (1, "1", True, "True")
            if confirmed:
                self._append_and_emit(candle)

    def _append_and_emit(self, candle: Candle) -> None:
        # Deduplicate consecutive identical candles
        if self.candles and self.candles[-1].start_time == candle.start_time:
            self.candles[-1] = candle
        else:
            self.candles.append(candle)

        if self._on_candle is not None:
            # Schedule the async callback on the running event loop.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(self._on_candle(candle))
