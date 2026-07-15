"""Tests for the live delta-neutral CarryStrategy (state machine + basis guard)."""
import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.carry_strategy import CarryConfig, CarryState, CarryStrategy


def _mock_exchange(funding=0.0002, perp=65000.0, spot=65000.0, equity=10000.0):
    ex = MagicMock()
    ex.get_funding_rate.return_value = {
        "fundingRate": str(funding),
        "markPrice": str(perp),
        "lastPrice": str(perp),
    }
    ex.get_spot_price.return_value = spot
    ex.get_wallet_balance.return_value = {
        "list": [{"coin": [{"coin": "USDT", "walletBalance": str(equity)}]}]
    }
    ex.place_order.return_value = {"orderId": "perp-1"}
    ex.place_spot_order.return_value = {"orderId": "spot-1"}
    return ex


# ---------------- entry logic --------------------

def test_opens_when_funding_favorable():
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(equity_fraction=0.5, qty_step=0.001))
    act = s.decide()
    assert act.action == "open"
    assert act.perp_side == "Sell"  # short perp
    assert act.spot_side == "Buy"  # long spot
    assert s.state == CarryState.HEDGED
    # notional = 0.5 * 10000 = 5000; qty = 5000/65000 = 0.0769 → floor to 0.076
    assert act.qty == 0.076


def test_stays_flat_when_funding_too_low():
    ex = _mock_exchange(funding=0.00005)  # below default min 0.0001
    s = CarryStrategy(ex)
    act = s.decide()
    assert act.action == "none"
    assert s.state == CarryState.FLAT


def test_no_open_when_zero_equity():
    ex = _mock_exchange(funding=0.0003, equity=0.0)
    s = CarryStrategy(ex)
    act = s.decide()
    assert act.action == "none"
    assert s.state == CarryState.FLAT


# ---------------- basis guard --------------------

def test_basis_guard_flattens_on_squeeze():
    """Perp premium > 50 bps while HEDGED → close (liquidation protection)."""
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(basis_guard_bps=50.0))
    s.decide()  # open
    assert s.state == CarryState.HEDGED
    # Squeeze: perp +1% above spot = 100 bps
    ex.get_funding_rate.return_value = {
        "fundingRate": "0.0003", "markPrice": "65650", "lastPrice": "65650"
    }
    ex.get_spot_price.return_value = 65000.0
    act = s.decide()
    assert act.action == "close"
    assert "basis guard" in act.reason
    assert s.state == CarryState.FLAT


def test_holds_when_basis_within_guard():
    """Small premium (< guard) while HEDGED → keep holding."""
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(basis_guard_bps=50.0, rebalance_drift_bps=200.0))
    s.decide()  # open at basis 0
    ex.get_funding_rate.return_value = {
        "fundingRate": "0.0003", "markPrice": "65195", "lastPrice": "65195"
    }  # +30 bps
    ex.get_spot_price.return_value = 65000.0
    act = s.decide()
    assert act.action == "none"
    assert s.state == CarryState.HEDGED


# ---------------- funding-sign exit --------------------

def test_exits_when_funding_turns_negative():
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(close_funding=-0.0001, rebalance_drift_bps=200.0))
    s.decide()  # open
    # Funding flips strongly negative
    ex.get_funding_rate.return_value = {
        "fundingRate": "-0.0003", "markPrice": "65000", "lastPrice": "65000"
    }
    act = s.decide()
    assert act.action == "close"
    assert s.state == CarryState.FLAT


# ---------------- rebalance --------------------

def test_rebalance_on_hedge_drift():
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(rebalance_drift_bps=20.0, basis_guard_bps=200.0))
    s.decide()  # open at basis 0
    # Drift +30 bps (above 20 rebalance, below 200 guard)
    ex.get_funding_rate.return_value = {
        "fundingRate": "0.0003", "markPrice": "65195", "lastPrice": "65195"
    }
    ex.get_spot_price.return_value = 65000.0
    act = s.decide()
    assert act.action == "rebalance"
    assert s.state == CarryState.HEDGED  # stays hedged


# ---------------- execution --------------------

def test_execute_open_places_both_legs():
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(equity_fraction=0.5, qty_step=0.001))
    act = s.decide()
    res = s.execute(act)
    assert act.action == "open"
    assert res is not None
    ex.place_order.assert_called_once()  # perp short
    ex.place_spot_order.assert_called_once()  # spot long
    # perp order is a Sell (short) — params passed as a positional dict
    assert ex.place_order.call_args.args[0]["side"] == "Sell"
    # spot Market BUY qty is in USDT (quote), not BTC (base)
    # qty_btc=0.076, price=65000 → spot qty ≈ 4940 USDT
    spot_qty = float(ex.place_spot_order.call_args.args[0]["qty"])
    assert 4900 < spot_qty < 5000  # ≈ 0.076 × 65000


def test_run_once_decides_and_executes():
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(equity_fraction=0.5, qty_step=0.001))
    act = s.run_once()
    assert act.action == "open"
    assert ex.place_order.called


# ---------------- trade logging --------------------

def test_trade_log_written_on_open(tmp_path: Path):
    log_path = str(tmp_path / "trades.csv")
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(equity_fraction=0.5, qty_step=0.001, trade_log=log_path))
    s.run_once()
    assert Path(log_path).exists()
    with open(log_path) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["action"] == "open"
    assert float(rows[0]["funding_rate"]) == 0.0003
    assert float(rows[0]["perp_price"]) == 65000.0


def test_no_trade_log_when_disabled(tmp_path: Path):
    log_path = str(tmp_path / "trades.csv")
    ex = _mock_exchange(funding=0.0003)
    s = CarryStrategy(ex, CarryConfig(equity_fraction=0.5, qty_step=0.001))
    s.run_once()
    assert not Path(log_path).exists()


def test_trade_log_close_appends_row(tmp_path: Path):
    log_path = str(tmp_path / "trades.csv")
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(equity_fraction=0.5, qty_step=0.001, trade_log=log_path))
    s.run_once()
    ex.get_funding_rate.return_value = {
        "fundingRate": "-0.0002", "markPrice": "65000", "lastPrice": "65000"
    }
    s.run_once()
    with open(log_path) as f:
        rows = list(csv.DictReader(f))
    actions = [r["action"] for r in rows]
    assert "open" in actions
    assert "close" in actions


# ---------------- rollback safety --------------------

def test_open_rolls_back_perp_if_spot_fails():
    """If the spot hedge fails, the perp short must be closed immediately."""
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    ex.place_spot_order.side_effect = RuntimeError("spot failed")
    s = CarryStrategy(ex, CarryConfig(equity_fraction=0.5, qty_step=0.001))
    act = s.decide()
    assert act.action == "open"
    with pytest.raises(RuntimeError, match="spot failed"):
        s.execute(act)
    assert s.state == CarryState.FLAT
    assert s.position_qty == 0.0
    assert ex.place_order.call_count == 2
    close_call = ex.place_order.call_args_list[1]
    assert close_call.args[0]["side"] == "Buy"
    assert close_call.args[0]["reduceOnly"] is True
