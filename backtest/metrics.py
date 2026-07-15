"""M2 — Performance metrics from an equity curve / trade list.

All functions are pure (numpy/pandas) so they're fast and easy to unit-test.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class PerformanceReport:
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    n_trades: int
    avg_trade: float


def equity_curve(returns: pd.Series) -> pd.Series:
    """Cumulative equity from per-bar simple returns, starting at 1.0."""
    return (1.0 + returns).cumprod()


def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough drop as a positive fraction (0.25 = -25%)."""
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return float(dd.min())  # negative number


def sharpe_ratio(returns: pd.Series, periods_per_year: int = 365 * 24) -> float:
    """Annualized Sharpe assuming risk-free = 0."""
    if returns.std() == 0:
        return 0.0
    return float(returns.mean() / returns.std() * np.sqrt(periods_per_year))


def sortino_ratio(returns: pd.Series, periods_per_year: int = 365 * 24) -> float:
    """Annualized Sortino (only penalizes downside deviation)."""
    downside = returns[returns < 0]
    if downside.std() == 0:
        return 0.0
    return float(returns.mean() / downside.std() * np.sqrt(periods_per_year))


def trade_stats(pnl_per_trade: np.ndarray) -> tuple[float, float, float]:
    """Return (win_rate, profit_factor, avg_trade)."""
    if len(pnl_per_trade) == 0:
        return 0.0, 0.0, 0.0
    wins = pnl_per_trade[pnl_per_trade > 0]
    losses = pnl_per_trade[pnl_per_trade < 0]
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    win_rate = len(wins) / len(pnl_per_trade)
    avg = float(pnl_per_trade.mean())
    return float(win_rate), float(pf), avg


def report(equity: pd.Series, pnl_per_trade: np.ndarray,
           periods_per_year: int = 365 * 24) -> PerformanceReport:
    """Build a full PerformanceReport from an equity curve + per-trade PnL."""
    returns = equity.pct_change().fillna(0.0)
    total_ret = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    n_periods = len(equity)
    years = n_periods / periods_per_year if periods_per_year else 1.0
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1.0 / max(years, 1e-9)) - 1.0
    win_rate, pf, avg = trade_stats(pnl_per_trade)
    return PerformanceReport(
        total_return=total_ret,
        cagr=float(cagr),
        sharpe=sharpe_ratio(returns, periods_per_year),
        sortino=sortino_ratio(returns, periods_per_year),
        max_drawdown=max_drawdown(equity),
        win_rate=win_rate,
        profit_factor=pf,
        n_trades=len(pnl_per_trade),
        avg_trade=avg,
    )
