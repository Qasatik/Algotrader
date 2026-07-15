"""Delta-neutral funding-rate carry backtest.

Strategy: when the perpetual funding rate is extreme, take a delta-neutral
position (perpetual + opposite spot leg) to collect the funding cash flow
without directional price exposure. Bybit pays funding every 8h on linear
perpetuals.

    rate > 0 → longs pay shorts  → SHORT perp + LONG spot → collect +rate
    rate < 0 → shorts pay longs  → LONG perp + SHORT spot → collect -rate

A correctly-positioned carry trade therefore collects ``|rate|`` per funding
event. The only frictions are exchange fees (perp + spot, entry + exit) and
slippage. Price risk is hedged by the opposite spot leg, so this is *not* a
directional bet — it is a pure carry/cash-flow trade.

NOTE on the short-spot leg: when ``rate < 0`` the hedge is a spot short, which
requires borrowing the asset (margin lending). That borrow cost is *not*
modelled here; in practice BTC funding is almost always positive, so the
``rate > 0`` branch (short perp + long spot) dominates and is fully executable
on a standard spot account.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from backtest.metrics import max_drawdown, sharpe_ratio, sortino_ratio

# Bybit funds 3x/day (00:00, 08:00, 16:00 UTC) → 1095 events/year.
FUNDING_EVENTS_PER_YEAR = 3 * 365


@dataclass
class CarryConfig:
    """Parameters for the funding carry backtest.

    All monetary quantities are fractions of notional (0.0001 == 0.01%).
    """

    entry_threshold: float = 0.0003  # enter when |funding| >= 0.03%
    exit_threshold: float = 0.0001  # exit when |funding| < 0.01%
    max_hold_events: int = 10  # cap holding period (funding events)
    perp_fee: float = 0.00055  # perp taker 0.055%
    spot_fee: float = 0.0010  # spot taker 0.10%
    slippage: float = 0.0002  # extra slippage per leg

    @property
    def round_trip_cost(self) -> float:
        """Total cost to open + close the delta-neutral pair (per unit notional)."""
        leg = self.perp_fee + self.spot_fee + self.slippage
        return leg * 2.0  # entry leg + exit leg


@dataclass
class CarryResult:
    """Outcome of a single carry backtest run."""

    total_return: float
    cagr: float
    n_trades: int
    win_rate: float
    profit_factor: float
    avg_funding_collected: float
    avg_hold_events: float
    sharpe: float
    sortino: float
    max_drawdown: float
    time_in_market: float
    equity: np.ndarray
    trade_pnls: np.ndarray


def run_carry_backtest(rates: pd.Series, cfg: CarryConfig | None = None) -> CarryResult:
    """Run the delta-neutral funding carry strategy.

    Args:
        rates: funding rates indexed by timestamp (8h frequency). Values are the
            raw funding rate (e.g. 0.0001 == 0.01%).
        cfg: strategy / cost configuration.

    Returns:
        CarryResult with equity curve, per-trade PnL and summary metrics.

    Timing (no look-ahead): at event ``i`` we observe ``rate[i]`` and may *enter*.
    Collection only begins at event ``i+1``, because funding at ``i`` was already
    settled before we could hold the position.
    """
    cfg = cfg or CarryConfig()
    r = rates.to_numpy(dtype="float64")
    n = len(r)

    equity = np.ones(n)
    eq = 1.0
    trade_pnls: list[float] = []

    entry_cost = cfg.round_trip_cost / 2.0
    exit_cost = cfg.round_trip_cost / 2.0

    in_position = False
    hold_count = 0
    collected_this_trade = 0.0
    events_in_market = 0

    for i in range(n):
        rate = r[i]
        if in_position:
            # Correctly positioned → receive |rate| (funding cash flow).
            gain = abs(rate)
            eq *= 1.0 + gain
            collected_this_trade += gain
            hold_count += 1
            events_in_market += 1
            # Exit when funding normalises or max hold reached.
            if abs(rate) < cfg.exit_threshold or hold_count >= cfg.max_hold_events:
                eq *= 1.0 - exit_cost
                trade_pnls.append(collected_this_trade - cfg.round_trip_cost)
                in_position = False
                collected_this_trade = 0.0
        elif abs(rate) >= cfg.entry_threshold:
            # Enter a fresh delta-neutral position (pay entry half of costs).
            eq *= 1.0 - entry_cost
            in_position = True
            hold_count = 0
        equity[i] = eq

    # Close any still-open position at the end of the sample.
    if in_position:
        eq *= 1.0 - exit_cost
        trade_pnls.append(collected_this_trade - cfg.round_trip_cost)
        equity[-1] = eq

    pnl_arr = np.array(trade_pnls, dtype="float64") if trade_pnls else np.zeros(0)
    eq_series = pd.Series(equity)
    rets = eq_series.pct_change().fillna(0.0)

    total_return = float(equity[-1] - 1.0)
    years = n / FUNDING_EVENTS_PER_YEAR
    cagr = float(equity[-1] ** (1.0 / years) - 1.0) if years > 0 and equity[-1] > 0 else -1.0

    wins = pnl_arr[pnl_arr > 0]
    losses = pnl_arr[pnl_arr <= 0]
    win_rate = float((pnl_arr > 0).mean()) if pnl_arr.size else 0.0
    profit_factor = (
        float(wins.sum() / abs(losses.sum())) if losses.size and losses.sum() != 0 else float("inf")
    )
    avg_funding = float(pnl_arr.mean()) if pnl_arr.size else 0.0

    return CarryResult(
        total_return=total_return,
        cagr=cagr,
        n_trades=int(pnl_arr.size),
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_funding_collected=avg_funding,
        avg_hold_events=float(events_in_market / max(pnl_arr.size, 1)),
        sharpe=sharpe_ratio(rets, FUNDING_EVENTS_PER_YEAR),
        sortino=sortino_ratio(rets, FUNDING_EVENTS_PER_YEAR),
        max_drawdown=max_drawdown(eq_series),
        time_in_market=float(events_in_market / n),
        equity=equity,
        trade_pnls=pnl_arr,
    )


def run_passive_carry(rates: pd.Series, cfg: CarryConfig | None = None) -> CarryResult:
    """Always-in delta-neutral carry: short perp + long spot from start to end.

    Collects the *signed* funding rate each event (so it eats negative-funding
    events rather than trying to time them) and pays a single round-trip cost.
    This is the realistic passive baseline — the carry yield of simply holding
    a hedged position, with no market timing.
    """
    cfg = cfg or CarryConfig()
    r = rates.to_numpy(dtype="float64")
    n = len(r)
    equity = np.ones(n)
    eq = 1.0 - cfg.round_trip_cost / 2.0  # enter once (pay entry half)
    for i in range(n):
        eq *= 1.0 + r[i]  # signed funding: +rate collected, -rate paid
        equity[i] = eq
    eq *= 1.0 - cfg.round_trip_cost / 2.0  # exit once (pay exit half)
    equity[-1] = eq

    eq_series = pd.Series(equity)
    rets = eq_series.pct_change().fillna(0.0)
    years = n / FUNDING_EVENTS_PER_YEAR
    cagr = float(equity[-1] ** (1.0 / years) - 1.0) if years > 0 and equity[-1] > 0 else -1.0

    return CarryResult(
        total_return=float(equity[-1] - 1.0),
        cagr=cagr,
        n_trades=1,
        win_rate=1.0 if equity[-1] > 1.0 else 0.0,
        profit_factor=float("inf") if equity[-1] > 1.0 else 0.0,
        avg_funding_collected=float(r.mean()),
        avg_hold_events=float(n),
        sharpe=sharpe_ratio(rets, FUNDING_EVENTS_PER_YEAR),
        sortino=sortino_ratio(rets, FUNDING_EVENTS_PER_YEAR),
        max_drawdown=max_drawdown(eq_series),
        time_in_market=1.0,
        equity=equity,
        trade_pnls=np.array([float(equity[-1] - 1.0 - cfg.round_trip_cost)]),
    )


def theoretical_max_carry(rates: pd.Series) -> float:
    """Upper bound: collect |rate| every event with zero cost (perfect foresight).

    Unreachable in practice (would require free sign-flips every 8h) but shows
    the gross magnitude of the funding cash flows.
    """
    return float(np.abs(rates.to_numpy(dtype="float64")).sum())
