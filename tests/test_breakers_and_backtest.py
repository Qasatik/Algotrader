"""Tests for risk circuit breakers (R1/R2) and backtest metrics."""
import numpy as np
import pandas as pd

from backtest.metrics import (
    max_drawdown,
    report,
    sharpe_ratio,
    trade_stats,
)
from core.risk_manager import RiskManager
from core.strategy import Side, Signal


def _mock_exchange(equity: float = 10000.0):
    from unittest.mock import MagicMock

    ex = MagicMock()
    ex.get_wallet_balance.return_value = {
        "list": [{"coin": [{"coin": "USDT", "walletBalance": str(equity)}]}]
    }
    ex.get_positions.return_value = []
    return ex


def _signal(price: float = 50000.0) -> Signal:
    return Signal(Side.BUY, 0.8, price, price * 0.996, price * 1.008, "t")


# ---------------- Risk circuit breakers (R1/R2) --------------------

def test_daily_drawdown_breaker_halts_trading():
    """After a 3% drop from day open, approve() must refuse."""
    ex = _mock_exchange(equity=10000.0)
    rm = RiskManager(ex)
    # First call seeds day_start_equity at 10000
    assert rm.approve(_signal()) is not None
    # Simulate equity dropping 4% -> beyond 3% limit
    ex.get_wallet_balance.return_value = {
        "list": [{"coin": [{"coin": "USDT", "walletBalance": "9600"}]}]
    }
    assert rm.approve(_signal()) is None
    assert rm.halted_reason is not None


def test_consecutive_losses_trigger_cooldown():
    ex = _mock_exchange()
    rm = RiskManager(ex)
    rm.max_consecutive_losses = 3
    for _ in range(3):
        rm.register_closed_trade(-50.0)
    assert rm.halted_reason is not None
    assert "cooldown" in rm.halted_reason.lower() or "consecutive" in rm.halted_reason.lower()
    # approve must now be blocked by cooldown
    assert rm.approve(_signal()) is None


def test_winning_trade_resets_loss_streak():
    ex = _mock_exchange()
    rm = RiskManager(ex)
    rm.max_consecutive_losses = 3
    rm.register_closed_trade(-10.0)
    rm.register_closed_trade(-10.0)
    rm.register_closed_trade(50.0)  # winner resets
    assert rm.halted_reason is None or "cooldown" not in (rm.halted_reason or "").lower()


# ---------------- Backtest metrics (M2) ----------------------------

def test_max_drawdown_calculation():
    equity = pd.Series([1.0, 1.2, 0.9, 1.1])  # peak 1.2 -> trough 0.9 = -25%
    dd = max_drawdown(equity)
    assert abs(dd - (-0.25)) < 1e-9


def test_sharpe_positive_for_good_returns():
    rets = pd.Series(np.random.RandomState(0).normal(0.001, 0.01, 1000))
    assert sharpe_ratio(rets) > 0


def test_trade_stats_basic():
    pnl = np.array([100.0, -50.0, 80.0, -20.0])
    win_rate, pf, avg = trade_stats(pnl)
    assert win_rate == 0.5
    assert pf > 1.0
    assert avg > 0


def test_report_smoke():
    eq = pd.Series(np.cumprod(1 + np.random.RandomState(1).normal(0.0005, 0.005, 500)))
    pnl = np.array([0.01, -0.005, 0.02, -0.01])
    rep = report(eq, pnl)
    assert rep.n_trades == 4
    assert -1.0 <= rep.max_drawdown <= 0.0
