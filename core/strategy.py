"""Trading strategy that fuses the ML prediction with order-book microstructure.

The strategy is event-driven: it is called on every *closed* candle. It:
  1. asks the InferenceEngine for a direction + confidence,
  2. cross-checks the signal against the live order-book imbalance,
  3. emits a concrete ``Signal`` (BUY / SELL / HOLD) for the order manager.

Keeping the strategy separate from execution makes it trivial to backtest
or swap in alternative logic later.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from config.settings import get_settings
from core.data_feed import Candle, MarketDataFeed
from ml.inference import Direction, InferenceEngine, Prediction
from utils.logger import get_logger

log = get_logger("strategy")


class Side(str, Enum):
    BUY = "Buy"
    SELL = "Sell"
    HOLD = "Hold"


@dataclass
class Signal:
    side: Side
    confidence: float
    entry_price: float
    # Suggested stop-loss and take-profit prices (risk manager may refine).
    stop_loss: float
    take_profit: float
    reason: str


class MlOrderbookStrategy:
    """Combine ML direction with order-book imbalance confirmation."""

    def __init__(
        self, engine: InferenceEngine, feed: MarketDataFeed
    ) -> None:
        self.engine = engine
        self.feed = feed
        s = get_settings()
        # ATR-style stop distance as a fraction of price.
        self.stop_frac = 0.004   # 0.4% fallback when ATR unavailable
        self.reward_risk = 2.0   # TP = RR * stop distance
        self.atr_stop_mult = 1.5  # S3: stop = ATR * mult (volatility-scaled)
        self.imbalance_min = 0.10  # require >=10% book skew to confirm
        self._log = log.bind(symbol=s.trading_symbol)

    def _atr(self, period: int = 14) -> float | None:
        """Average True Range over the last `period` candles (S3).

        True Range = max(high-low, |high-prev_close|, |low-prev_close|).
        Returns None if there isn't enough history.
        """
        candles = list(self.feed.candles)
        if len(candles) < period + 1:
            return None
        trs = []
        prev = candles[-(period + 1)]
        for c in candles[-period:]:
            tr = max(
                c.high - c.low,
                abs(c.high - prev.close),
                abs(c.low - prev.close),
            )
            trs.append(tr)
            prev = c
        return sum(trs) / len(trs)

    async def on_candle(self, candle: Candle) -> Signal | None:
        """Evaluate a closed candle and return a Signal (or None to skip)."""
        closes = self.feed.latest_closes()
        volumes = [c.volume for c in list(self.feed.candles)[-len(closes):]]

        prediction: Prediction | None = self.engine.predict(closes, volumes)
        if prediction is None:
            return None

        price = self.feed.book.mid_price or candle.close

        # Order-book imbalance: (bid_vol - ask_vol) / (bid_vol + ask_vol)
        imb = self._orderbook_imbalance()

        side = Side.HOLD
        reason = "no_signal"

        if prediction.direction == Direction.UP and prediction.confidence >= self.engine.threshold:
            if imb >= -self.imbalance_min:  # book not strongly against us
                side = Side.BUY
                reason = f"ml_up conf={prediction.confidence:.2f} imb={imb:+.2f}"
        elif prediction.direction == Direction.DOWN and prediction.confidence >= self.engine.threshold:
            if imb <= self.imbalance_min:
                side = Side.SELL
                reason = f"ml_down conf={prediction.confidence:.2f} imb={imb:+.2f}"

        if side == Side.HOLD:
            self._log.debug(
                "hold",
                direction=prediction.direction.name,
                confidence=round(prediction.confidence, 3),
                imbalance=round(imb, 3),
            )
            return None

        # S3: ATR-based adaptive stop distance (volatility-scaled).
        atr = self._atr(period=14) or price * self.stop_frac
        stop_dist = atr * self.atr_stop_mult
        if side == Side.BUY:
            sl = price - stop_dist
            tp = price + stop_dist * self.reward_risk
        else:
            sl = price + stop_dist
            tp = price - stop_dist * self.reward_risk

        signal = Signal(
            side=side,
            confidence=prediction.confidence,
            entry_price=price,
            stop_loss=sl,
            take_profit=tp,
            reason=reason,
        )
        self._log.info(
            "signal",
            side=side.value,
            price=price,
            sl=round(sl, 2),
            tp=round(tp, 2),
            reason=reason,
        )
        return signal

    def _orderbook_imbalance(self) -> float:
        """Volume imbalance between top bids and asks in [-1, 1]."""
        bids = self.feed.book.bids
        asks = self.feed.book.asks
        if not bids or not asks:
            return 0.0
        try:
            bid_vol = sum(float(b[1]) for b in bids[:10])
            ask_vol = sum(float(a[1]) for a in asks[:10])
        except (IndexError, ValueError):
            return 0.0
        total = bid_vol + ask_vol
        return 0.0 if total == 0 else (bid_vol - ask_vol) / total
