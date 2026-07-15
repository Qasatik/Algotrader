"""Bybit V5 exchange connection wrapper.

Wraps the official `pybit` HTTP client and adds:
  * automatic retry with exponential backoff (via tenacity),
  * latency measurement of each REST round-trip,
  * a thin async-friendly facade for the (synchronous) pybit session.

Bybit's matching engine is hosted on AWS in Singapore (ap-southeast-1).
For minimum latency, run this bot from a Singapore data center.
"""
from __future__ import annotations

import time
from typing import Any

from pybit.exceptions import InvalidRequestError
from pybit.unified_trading import HTTP
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import get_settings
from utils.logger import get_logger

log = get_logger("exchange")


class ExchangeError(Exception):
    """Raised when the exchange rejects a request after all retries."""


class BybitExchange:
    """Synchronous facade over the pybit HTTP session.

    pybit's HTTP client is thread-safe for independent calls, so we run it
    inside an asyncio executor (see ``async_to_thread`` usage in callers).
    """

    def __init__(self, testnet: bool | None = None) -> None:
        """Build the HTTP session.

        Args:
            testnet: Force testnet/mainnet. When ``None`` (default) the value
                comes from settings and ``assert_ready_for_live`` is enforced.
                Pass ``False`` to read *public* mainnet market data (klines,
                tickers) without credentials — used by the data pipeline.
        """
        s = get_settings()
        if testnet is None:
            testnet = s.is_paper_mode
            s.assert_ready_for_live()
        self._session: HTTP = HTTP(
            testnet=testnet,
            api_key=s.bybit_api_key or None,
            api_secret=s.bybit_api_secret or None,
            recv_window=5000,
            timeout=s.http_timeout,
        )
        self._log = log.bind(testnet=testnet)

    # ------------------------------------------------------------------
    # Low-level request with retry + latency tracking
    # ------------------------------------------------------------------
    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.2, min=0.2, max=3.0),
        reraise=True,
    )
    def _request(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """Call a pybit session method, measuring latency & checking retCode."""
        start = time.perf_counter()
        try:
            result: dict[str, Any] = getattr(self._session, method)(**kwargs)
        except InvalidRequestError as exc:
            # pybit raises this for API-level rejections (retCode != 0).
            # Convert to ExchangeError so callers can pattern-match error codes.
            raise ExchangeError(str(exc)) from exc
        except Exception as exc:  # network / HTTP error
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._log.warning(
                "request_failed", method=method, latency_ms=round(elapsed_ms, 2), error=str(exc)
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        ret_code = result.get("retCode", -1)
        if ret_code != 0:
            raise ExchangeError(f"{method} -> retCode={ret_code} msg={result.get('retMsg')}")

        self._log.debug(
            "request_ok", method=method, latency_ms=round(elapsed_ms, 2)
        )
        return result.get("result", {})

    # ------------------------------------------------------------------
    # Public market data
    # ------------------------------------------------------------------
    def get_ticker(self, symbol: str) -> dict[str, Any]:
        """Latest mark/last price for a symbol."""
        return self._request("get_tickers", category="linear", symbol=symbol)

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        start: int | None = None,
        end: int | None = None,
    ) -> list[list[str]]:
        """Historical OHLCV candles (newest-first).

        Returns rows: [start, open, high, low, close, volume, turnover].
        ``start``/``end`` are millisecond timestamps that bound the query and
        enable backward pagination for bulk downloads.
        """
        params: dict[str, Any] = {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        res = self._request("get_kline", **params)
        return res.get("list", [])

    def get_orderbook(self, symbol: str, limit: int = 50) -> dict[str, Any]:
        """Snapshot of the current order book."""
        return self._request(
            "get_orderbook", category="linear", symbol=symbol, limit=limit
        )

    def get_funding_rate(self, symbol: str) -> dict[str, Any]:
        """Current funding rate + next funding time for a linear perp.

        Returns a dict with ``fundingRate`` (fraction, e.g. 0.0001 == 0.01%),
        ``nextFundingTime`` (ms) and ``markPrice``.
        """
        res = self._request("get_tickers", category="linear", symbol=symbol)
        lst = res.get("list", [])
        return lst[0] if lst else {}

    def get_spot_price(self, symbol: str) -> float | None:
        """Last traded price on the spot market (for basis / hedge calc)."""
        res = self._request("get_tickers", category="spot", symbol=symbol)
        lst = res.get("list", [])
        if not lst:
            return None
        try:
            return float(lst[0].get("lastPrice", 0.0)) or None
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Account / trading
    # ------------------------------------------------------------------
    def get_wallet_balance(self, coin: str = "USDT") -> dict[str, Any]:
        """Unified account wallet balance."""
        res = self._request("get_wallet_balance", accountType="UNIFIED", coin=coin)
        return res

    def get_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Current open positions."""
        res = self._request(
            "get_positions", category="linear", symbol=symbol, settleCoin="USDT"
        )
        return res.get("list", [])

    def get_closed_pnl(
        self, symbol: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Closed-position PnL records (realised profit/loss history).

        Each record has: symbol, closedPnl, execQty, avgEntryPrice,
        avgExitPrice, createdTime, updatedTime, etc.
        """
        params: dict[str, Any] = {"category": "linear", "limit": limit}
        if symbol:
            params["symbol"] = symbol
        res = self._request("get_closed_pnl", **params)
        return res.get("list", [])

    def get_order_history(
        self, symbol: str | None = None, limit: int = 50, category: str = "linear"
    ) -> list[dict[str, Any]]:
        """Historical orders (filled, cancelled, etc.).

        Each record has: orderId, symbol, side, orderType, qty, cumExecQty,
        avgPrice, status, createdTime, etc.
        """
        params: dict[str, Any] = {"category": category, "limit": limit}
        if symbol:
            params["symbol"] = symbol
        res = self._request("get_order_history", **params)
        return res.get("list", [])

    def get_executions(
        self, symbol: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Trade execution records (every individual fill).

        Each record has: execId, symbol, side, execPrice, execQty,
        execFee, execType, tradeTime, etc.
        """
        params: dict[str, Any] = {"category": "linear", "limit": limit}
        if symbol:
            params["symbol"] = symbol
        res = self._request("get_executions", **params)
        return res.get("list", [])

    def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        """Configure leverage for a linear symbol."""
        try:
            return self._request(
                "set_leverage",
                category="linear",
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except ExchangeError as exc:
            # Bybit returns 110043 when leverage is already set to that value.
            if "110043" in str(exc):
                self._log.info("leverage_already_set", symbol=symbol, leverage=leverage)
                return {}
            raise

    def set_trading_stop(
        self, symbol: str, stop_loss: str | None = None,
        take_profit: str | None = None, trigger_by: str = "MarkPrice",
    ) -> dict[str, Any]:
        """Set / clear exchange-side stop-loss and take-profit for a position.

        These orders live on Bybit's servers and trigger **even when the bot
        is offline** — critical backstop protection.  Pass ``"0"`` to clear.

        For a SHORT position the stop-loss price should be *above* entry
        (price rising against the short).
        """
        params: dict[str, Any] = {
            "category": "linear",
            "symbol": symbol,
            "tpslMode": "Full",
            "slTriggerBy": trigger_by,
        }
        if stop_loss is not None:
            params["stopLoss"] = stop_loss
        if take_profit is not None:
            params["takeProfit"] = take_profit
        return self._request("set_trading_stop", **params)

    def place_order(self, params: dict[str, Any]) -> dict[str, Any]:
        """Submit an order. See OrderManager for how `params` is built."""
        return self._request("place_order", category="linear", **params)

    def place_spot_order(self, params: dict[str, Any]) -> dict[str, Any]:
        """Submit a spot order (the long hedge leg of the carry pair)."""
        return self._request("place_order", category="spot", **params)

    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return self._request(
            "cancel_order", category="linear", symbol=symbol, orderId=order_id
        )

    def close(self) -> None:
        """Release the underlying HTTP session."""
        try:
            self._session.client.close()  # type: ignore[attr-defined]
        except Exception:
            pass
