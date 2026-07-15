"""Order execution: translates an ApprovedTrade into Bybit orders.

Supports two entry modes (E1):
  * **Maker (default)**: post a PostOnly limit at the best bid/ask to *earn*
    Bybit's negative maker fee (rebate). If it doesn't fill within
    ``MAKER_REPRICE_TIMEOUT`` seconds, fall back to a taker market order so we
    never miss a signal.
  * **Taker**: immediate market entry (original behaviour).

In both modes the entry carries an attached stop-loss + take-profit bracket.
All blocking pybit calls run in a worker thread so the asyncio loop never stalls.
"""
from __future__ import annotations

import asyncio
from typing import Any

from config.settings import get_settings
from core.data_feed import MarketDataFeed
from core.exchange import BybitExchange
from core.risk_manager import ApprovedTrade
from core.strategy import Side
from utils.logger import get_logger

log = get_logger("orders")


class OrderManager:
    """Executes approved trades on Bybit with SL/TP brackets."""

    def __init__(self, exchange: BybitExchange, feed: MarketDataFeed | None = None) -> None:
        s = get_settings()
        self.exchange = exchange
        self.feed = feed
        self.symbol = s.trading_symbol
        self.use_maker = s.use_maker_orders
        self.maker_timeout = s.maker_reprice_timeout
        self._log = log.bind(symbol=self.symbol)

    async def execute(self, trade: ApprovedTrade) -> dict[str, Any] | None:
        """Place entry + SL/TP. Tries maker first, falls back to taker."""
        try:
            if self.use_maker:
                result = await self._try_maker_then_taker(trade)
            else:
                result = await asyncio.to_thread(self._place_market_bracket, trade)

            self._log.info(
                "order_filled",
                side=trade.side.value,
                qty=trade.qty,
                order_id=result.get("orderId"),
            )
            return result
        except Exception as exc:
            self._log.error("order_failed", error=str(exc), side=trade.side.value)
            return None

    # ------------------------------------------------------------------
    # Maker-first execution (E1)
    # ------------------------------------------------------------------
    async def _try_maker_then_taker(self, trade: ApprovedTrade) -> dict[str, Any]:
        """Post a maker limit; if unfilled, cancel and send a market bracket."""
        limit_price = self._maker_price(trade.side)
        if limit_price is None:
            # No book available -> go straight to taker.
            return await asyncio.to_thread(self._place_market_bracket, trade)

        order_id = await asyncio.to_thread(self._place_maker_limit, trade, limit_price)
        if order_id is None:
            return await asyncio.to_thread(self._place_market_bracket, trade)

        # Wait briefly for the maker order to fill.
        filled = await self._await_fill(order_id, self.maker_timeout)
        if filled:
            # Attach SL/TP as separate reduce-only orders after maker fill.
            await asyncio.to_thread(self._attach_stop_take, trade)
            return {"orderId": order_id, "filled": "maker"}

        # Not filled in time -> cancel and cross as taker.
        await asyncio.to_thread(self._cancel_silent, order_id)
        self._log.info("maker_unfilled_fallback_taker", order_id=order_id)
        return await asyncio.to_thread(self._place_market_bracket, trade)

    def _maker_price(self, side: Side) -> float | None:
        """Best bid for buys, best ask for sells (join the book as maker)."""
        if self.feed is None:
            return None
        if side == Side.BUY:
            px = self.feed.book.best_bid
        else:
            px = self.feed.book.best_ask
        return px

    def _place_maker_limit(self, trade: ApprovedTrade, price: float) -> str | None:
        """Post a PostOnly limit (guaranteed maker, never crosses)."""
        params = {
            "symbol": self.symbol,
            "side": trade.side.value,
            "orderType": "Limit",
            "qty": self._format_qty(trade.qty),
            "price": str(round(price, 6)),
            "timeInForce": "PostOnly",
            "reduceOnly": False,
            "category": "linear",
        }
        try:
            res = self.exchange.place_order(params)
            return res.get("orderId")
        except Exception as exc:
            self._log.warning("maker_place_failed", error=str(exc))
            return None

    async def _await_fill(self, order_id: str, timeout: float) -> bool:
        """Poll order status until filled or timeout (simplified)."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            status = await asyncio.to_thread(self._order_status, order_id)
            if status in ("Filled", "PartiallyFilled"):
                return True
            if status in ("Cancelled", "Rejected"):
                return False
            await asyncio.sleep(0.2)
        return False

    def _order_status(self, order_id: str) -> str:
        try:
            res = self.exchange._request(  # reuse retry-wrapped request
                "get_open_orders", symbol=self.symbol, orderId=order_id
            )
            orders = res.get("list", [])
            if orders:
                return orders[0].get("orderStatus", "New")
        except Exception:
            pass
        return "New"

    def _cancel_silent(self, order_id: str) -> None:
        try:
            self.exchange.cancel_order(order_id, self.symbol)
        except Exception:
            pass

    def _attach_stop_take(self, trade: ApprovedTrade) -> None:
        """Attach reduce-only SL and TP after a maker fill (two orders)."""
        base = {
            "symbol": self.symbol,
            "side": "Sell" if trade.side == Side.BUY else "Buy",
            "qty": self._format_qty(trade.qty),
            "reduceOnly": True,
            "category": "linear",
        }
        # Take profit
        try:
            self.exchange.place_order({
                **base, "orderType": "Limit",
                "price": str(round(trade.take_profit, 6)),
                "triggerBy": "LastPrice", "triggerPrice": str(round(trade.take_profit, 6)),
                "timeInForce": "GTC",
            })
        except Exception as exc:
            self._log.warning("attach_tp_failed", error=str(exc))
        # Stop loss
        try:
            self.exchange.place_order({
                **base, "orderType": "Market",
                "triggerBy": "LastPrice", "triggerPrice": str(round(trade.stop_loss, 6)),
                "timeInForce": "IOC",
            })
        except Exception as exc:
            self._log.warning("attach_sl_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Taker (market) bracket
    # ------------------------------------------------------------------
    def _place_market_bracket(self, trade: ApprovedTrade) -> dict[str, Any]:
        """Place a market order with attached TP/SL via Bybit V5."""
        params = {
            "symbol": self.symbol,
            "side": trade.side.value,
            "orderType": "Market",
            "qty": self._format_qty(trade.qty),
            "takeProfit": str(round(trade.take_profit, 6)),
            "stopLoss": str(round(trade.stop_loss, 6)),
            "tpTriggerBy": "LastPrice",
            "slTriggerBy": "LastPrice",
            "timeInForce": "IOC",
            "reduceOnly": False,
            "category": "linear",
        }
        return self.exchange.place_order(params)

    @staticmethod
    def _format_qty(qty: float) -> str:
        """Trim to a sane precision; Bybit requires valid step size per symbol."""
        return f"{qty:.4f}".rstrip("0").rstrip(".") or "0"
