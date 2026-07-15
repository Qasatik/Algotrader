"""Tests for the risk manager with a mocked exchange."""
from unittest.mock import MagicMock

from core.risk_manager import RiskManager
from core.strategy import Side, Signal


def _make_signal(side: Side, price: float) -> Signal:
    return Signal(
        side=side,
        confidence=0.8,
        entry_price=price,
        stop_loss=price * 0.996,   # 0.4% stop
        take_profit=price * 1.008,
        reason="test",
    )


def _mock_exchange(equity: float = 10000.0, open_positions: int = 0):
    ex = MagicMock()
    ex.get_wallet_balance.return_value = {
        "list": [{"coin": [{"coin": "USDT", "walletBalance": str(equity)}]}]
    }
    ex.get_positions.return_value = [
        {"size": "1"} for _ in range(open_positions)
    ]
    return ex


def test_approve_sizes_by_risk():
    ex = _mock_exchange(equity=10000.0)
    rm = RiskManager(ex)
    trade = rm.approve(_make_signal(Side.BUY, 50000.0))

    assert trade is not None
    # risk_amount = equity * risk_per_trade(0.01) = 100
    # sl_distance = 50000 * 0.004 = 200 -> qty = 100/200 = 0.5
    assert abs(trade.qty - 0.5) < 1e-6
    assert abs(trade.risk_amount - 100.0) < 1e-6


def test_approve_blocks_when_max_positions_reached():
    ex = _mock_exchange(equity=10000.0, open_positions=3)
    rm = RiskManager(ex)
    assert rm.approve(_make_signal(Side.BUY, 50000.0)) is None


def test_approve_blocks_hold():
    ex = _mock_exchange()
    rm = RiskManager(ex)
    assert rm.approve(_make_signal(Side.HOLD, 50000.0)) is None


def test_approve_blocks_zero_equity():
    ex = _mock_exchange(equity=0.0)
    rm = RiskManager(ex)
    assert rm.approve(_make_signal(Side.BUY, 50000.0)) is None
