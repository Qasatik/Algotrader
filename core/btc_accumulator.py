"""Auto-convert USDT funding profits to BTC spot (DCA into Bitcoin).

The carry strategy earns funding income in USDT.  This module periodically
sweeps the accumulated profit into BTC spot, so the portfolio gradually
accumulates Bitcoin without manual intervention.

Profit is measured via Bybit's ``cumRealisedPnl`` field (cumulative realised
P&L), which includes funding payments, trading fees, and realised gains/losses
— but **not** deposits.  This means:

* Deposits (adding capital) do **not** trigger BTC purchases.
* Only actual trading profit (funding + closed P&L) is converted.
* BTC price changes do **not** affect the measurement (USDT-denominated).

Usage::

    acc = BtcAccumulator(exchange, threshold_usdt=5.0)
    acc.init_baseline()                # call once at startup
    result = acc.check_and_convert()   # call periodically in the main loop
    if result:
        print(f"Bought {result['qty']} BTC for ${result['usdt_spent']}")
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.exchange import BybitExchange
from utils.logger import get_logger

log = get_logger("btc_accumulator")

_DEFAULT_STATE_FILE = "data/btc_accumulator_state.json"


@dataclass
class AccumulatorState:
    """Persistent state for the BTC accumulator (survives restarts)."""

    baseline_rpnl: float = 0.0
    """``cumRealisedPnl`` at init — the reference point for profit."""

    total_btc_bought: float = 0.0
    """Cumulative BTC purchased across all conversions."""

    total_usdt_invested: float = 0.0
    """Cumulative USDT spent on BTC across all conversions."""

    conversion_count: int = 0
    """Number of successful BTC purchases."""

    last_conversion_ts: str = ""
    """ISO timestamp of the most recent conversion."""

    last_rpnl: float = 0.0
    """Most recent ``cumRealisedPnl`` reading (for diagnostics)."""


@dataclass
class ConversionResult:
    """Returned when a BTC purchase is executed."""

    qty: float
    usdt_spent: float
    btc_price: float
    total_btc: float
    total_invested: float


class BtcAccumulator:
    """Sweep funding profits into BTC spot when they exceed a threshold.

    Parameters
    ----------
    exchange
        Live Bybit exchange wrapper (mainnet).
    threshold_usdt
        Minimum unconverted profit (USDT) required before buying BTC.
    min_free_reserve
        Always leave at least this much free USDT for trading/margin.
    state_file
        Path to the JSON file for persisting state across restarts.
    """

    def __init__(
        self,
        exchange: BybitExchange,
        threshold_usdt: float = 5.0,
        min_free_reserve: float = 10.0,
        state_file: str = _DEFAULT_STATE_FILE,
    ) -> None:
        self.exchange = exchange
        self.threshold_usdt = threshold_usdt
        self.min_free_reserve = min_free_reserve
        self.state_file = state_file
        self.state: AccumulatorState = AccumulatorState()
        self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        p = Path(self.state_file)
        if p.exists():
            try:
                data = json.loads(p.read_text())
                self.state = AccumulatorState(**data)
                log.info(
                    "btc_accumulator_loaded",
                    baseline_rpnl=self.state.baseline_rpnl,
                    total_btc=self.state.total_btc_bought,
                    conversions=self.state.conversion_count,
                )
            except Exception as exc:
                log.warning("btc_accumulator_load_failed", error=str(exc))

    def _save_state(self) -> None:
        p = Path(self.state_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.state.__dict__, indent=2))

    # ------------------------------------------------------------------
    # Exchange reads
    # ------------------------------------------------------------------
    def _get_rpnl(self) -> float:
        """Read cumulative realised P&L from the USDT coin row."""
        res = self.exchange.get_wallet_balance("USDT")
        coin = res["list"][0]["coin"][0]
        return float(coin.get("cumRealisedPnl", 0.0))

    def _get_free_usdt(self) -> float:
        """Read available (free) USDT balance."""
        res = self.exchange.get_wallet_balance("USDT")
        acct = res["list"][0]
        return float(acct.get("totalAvailableBalance", 0.0))

    def _get_btc_spot_step(self) -> float:
        """Lot-size step for BTCUSDT spot (e.g. 0.00001)."""
        try:
            info = self.exchange.get_instrument_info("BTCUSDT", category="spot")
            lot = info.get("lotSizeFilter", {})
            step = float(lot.get("basePrecision", "0.00001"))
            return step if step > 0 else 0.00001
        except Exception:
            return 0.00001

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def init_baseline(self) -> None:
        """Record the starting ``cumRealisedPnl`` (call once at startup).

        If a baseline was already loaded from the state file, it is kept
        (so restarts don't reset the profit counter).
        """
        current = self._get_rpnl()
        self.state.last_rpnl = current
        if self.state.baseline_rpnl == 0.0 and self.state.conversion_count == 0:
            self.state.baseline_rpnl = current
            self._save_state()
            log.info("btc_accumulator_init", baseline_rpnl=current)
        else:
            log.info(
                "btc_accumulator_baseline_exists",
                baseline_rpnl=self.state.baseline_rpnl,
                current_rpnl=current,
                profit=current - self.state.baseline_rpnl,
            )

    def reset_baseline(self) -> None:
        """Reset the baseline to the current P&L (use after manual interventions)."""
        current = self._get_rpnl()
        self.state.baseline_rpnl = current
        self.state.total_usdt_invested = 0.0
        self.state.last_rpnl = current
        self._save_state()
        log.info("btc_accumulator_reset", baseline_rpnl=current)

    def status(self) -> dict[str, Any]:
        """Return a diagnostic snapshot (for display / Telegram)."""
        try:
            rpnl = self._get_rpnl()
        except Exception:
            rpnl = self.state.last_rpnl
        profit = rpnl - self.state.baseline_rpnl
        unconverted = profit - self.state.total_usdt_invested
        return {
            "baseline_rpnl": round(self.state.baseline_rpnl, 4),
            "current_rpnl": round(rpnl, 4),
            "total_profit": round(profit, 4),
            "unconverted_profit": round(unconverted, 4),
            "threshold": self.threshold_usdt,
            "total_btc_bought": self.state.total_btc_bought,
            "total_usdt_invested": round(self.state.total_usdt_invested, 2),
            "conversion_count": self.state.conversion_count,
            "last_conversion": self.state.last_conversion_ts,
        }

    def check_and_convert(self) -> ConversionResult | None:
        """Check if enough profit has accumulated; if so, buy BTC.

        Returns a :class:`ConversionResult` if BTC was purchased, ``None``
        otherwise.  Safe to call every poll cycle — it short-circuits when
        the threshold isn't met.
        """
        try:
            rpnl = self._get_rpnl()
        except Exception as exc:
            log.warning("btc_accumulator_rpnl_read_failed", error=str(exc))
            return None

        self.state.last_rpnl = rpnl
        profit = rpnl - self.state.baseline_rpnl
        unconverted = profit - self.state.total_usdt_invested

        if unconverted < self.threshold_usdt:
            return None

        # Don't spend more than what's available (keep reserve for trading).
        free_usdt = self._get_free_usdt()
        buy_amount = min(unconverted, max(free_usdt - self.min_free_reserve, 0.0))

        if buy_amount < self.threshold_usdt:
            log.info(
                "btc_accumulator_skip_low_free",
                unconverted=round(unconverted, 2),
                free_usdt=round(free_usdt, 2),
                reserve=self.min_free_reserve,
            )
            return None

        return self._buy_btc(buy_amount)

    # ------------------------------------------------------------------
    # Internal: execute the spot buy
    # ------------------------------------------------------------------
    def _buy_btc(self, usdt_amount: float) -> ConversionResult | None:
        """Place a market buy order for BTCUSDT spot."""
        btc_price = self.exchange.get_spot_price("BTCUSDT")
        if not btc_price or btc_price <= 0:
            log.warning("btc_accumulator_no_price")
            return None

        step = self._get_btc_spot_step()
        raw_qty = usdt_amount / btc_price
        # Round DOWN to the nearest lot step (never over-spend).
        qty = (raw_qty // step) * step

        if qty < step:
            log.info(
                "btc_accumulator_skip_tiny",
                raw_qty=raw_qty,
                step=step,
                usdt_amount=round(usdt_amount, 2),
            )
            return None

        order_link_id = f"btc-accum-{int(time.time() * 1000)}"[-36:]
        try:
            self.exchange.place_spot_order({
                "symbol": "BTCUSDT",
                "side": "Buy",
                "orderType": "Market",
                "qty": str(qty),
                "orderLinkId": order_link_id,
            })
        except Exception as exc:
            log.error("btc_accumulator_buy_failed", error=str(exc), qty=qty)
            return None

        spent = qty * btc_price
        self.state.total_btc_bought = round(self.state.total_btc_bought + qty, 8)
        self.state.total_usdt_invested = round(self.state.total_usdt_invested + spent, 4)
        self.state.conversion_count += 1
        self.state.last_conversion_ts = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        self._save_state()

        log.info(
            "btc_accumulator_bought",
            btc_qty=qty,
            usdt_spent=round(spent, 2),
            btc_price=btc_price,
            total_btc=self.state.total_btc_bought,
            total_invested=round(self.state.total_usdt_invested, 2),
            count=self.state.conversion_count,
        )
        print(
            f"  ₿ BTC ACCUMULATOR: bought {qty:.8f} BTC for ${spent:.2f} "
            f"(@ ${btc_price:.2f}) | total: {self.state.total_btc_bought:.8f} BTC"
        )

        return ConversionResult(
            qty=qty,
            usdt_spent=spent,
            btc_price=btc_price,
            total_btc=self.state.total_btc_bought,
            total_invested=self.state.total_usdt_invested,
        )
