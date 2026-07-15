"""Tests for the live delta-neutral CarryStrategy (state machine + basis guard)."""
import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.carry_strategy import CarryAction, CarryConfig, CarryState, CarryStrategy


def _mock_exchange(funding=0.0002, perp=65000.0, spot=65000.0, equity=10000.0,
                   perp_size=0.0, spot_btc=0.0):
    ex = MagicMock()
    ex.get_funding_rate.return_value = {
        "fundingRate": str(funding),
        "markPrice": str(perp),
        "lastPrice": str(perp),
    }
    ex.get_spot_price.return_value = spot
    ex.get_wallet_balance.return_value = {
        "list": [{"coin": [
            {"coin": "USDT", "walletBalance": str(equity)},
            {"coin": "BTC", "walletBalance": str(spot_btc)},
        ]}]
    }
    ex.get_positions.return_value = (
        [{"symbol": "BTCUSDT", "size": str(perp_size), "side": "Sell"}]
        if perp_size else []
    )
    ex.place_order.return_value = {"orderId": "perp-1"}
    ex.place_spot_order.return_value = {"orderId": "spot-1"}
    return ex


# ---------------- entry logic --------------------

def test_opens_when_funding_favorable():
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(
        equity_fraction=0.5, qty_step=0.001, size_mult_min=1.0, size_mult_max=1.0))
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
    """Severe negative funding (projected loss > close cost) triggers exit."""
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(
        close_funding=-0.0001, rebalance_drift_bps=200.0, exit_confirm_polls=1))
    s.decide()  # open
    # -0.0004 × 10 cycles × 10000 = 40 bps projected loss > 31 bps close cost
    ex.get_funding_rate.return_value = {
        "fundingRate": "-0.0004", "markPrice": "65000", "lastPrice": "65000"
    }
    act = s.decide()
    assert act.action == "close"
    assert s.state == CarryState.FLAT


def test_mild_negative_funding_holds():
    """Negative funding below threshold but projected loss < close cost → hold."""
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(
        close_funding=-0.0001, rebalance_drift_bps=200.0, exit_confirm_polls=1))
    s.decide()  # open
    # -0.0002 × 10 × 10000 = 20 bps < 31 bps → not worth closing
    ex.get_funding_rate.return_value = {
        "fundingRate": "-0.0002", "markPrice": "65000", "lastPrice": "65000"
    }
    act = s.decide()
    assert act.action == "none"
    assert s.state == CarryState.HEDGED


def test_exit_requires_confirmation():
    """Severe negative funding requires exit_confirm_polls consecutive warrants."""
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(
        close_funding=-0.0001, rebalance_drift_bps=200.0, exit_confirm_polls=3))
    s.decide()  # open
    ex.get_funding_rate.return_value = {
        "fundingRate": "-0.0004", "markPrice": "65000", "lastPrice": "65000"
    }
    assert s.decide().action == "none"  # poll 1/3
    assert s.decide().action == "none"  # poll 2/3
    act = s.decide()  # poll 3/3 → confirmed
    assert act.action == "close"
    assert s.state == CarryState.FLAT


def test_exit_counter_resets_on_recovery():
    """If funding recovers before confirmation, the exit counter resets."""
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(
        close_funding=-0.0001, rebalance_drift_bps=200.0, exit_confirm_polls=3))
    s.decide()  # open
    ex.get_funding_rate.return_value = {
        "fundingRate": "-0.0004", "markPrice": "65000", "lastPrice": "65000"
    }
    s.decide()  # 1/3
    s.decide()  # 2/3
    # Funding recovers → counter resets
    ex.get_funding_rate.return_value = {
        "fundingRate": "0.0002", "markPrice": "65000", "lastPrice": "65000"
    }
    s.decide()
    assert s._exit_signals == 0
    # Severe again — should need 3 more, not just 1
    ex.get_funding_rate.return_value = {
        "fundingRate": "-0.0004", "markPrice": "65000", "lastPrice": "65000"
    }
    assert s.decide().action == "none"  # 1/3 again


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


def test_rebalance_buys_spot_when_net_short():
    """Perp short larger than spot long → buy more spot (USDT qty)."""
    ex = _mock_exchange(perp=65000.0, spot=65000.0, perp_size=0.002, spot_btc=0.001)
    s = CarryStrategy(ex, CarryConfig(rebalance_min_btc=0.001, basis_guard_bps=200.0))
    act = CarryAction("rebalance", "drift", spot_price=65000.0, perp_price=65000.0)
    res = s._rebalance(act)
    assert res["rebalanced"] is True
    ex.place_spot_order.assert_called_once()
    order = ex.place_spot_order.call_args.args[0]
    assert order["side"] == "Buy"
    # delta = 0.001 BTC × 65000 = 65.0 USDT
    assert float(order["qty"]) == 65.0


def test_rebalance_sells_spot_when_net_long():
    """Spot long larger than perp short → sell spot (BTC qty)."""
    ex = _mock_exchange(perp=65000.0, spot=65000.0, perp_size=0.001, spot_btc=0.002)
    s = CarryStrategy(ex, CarryConfig(rebalance_min_btc=0.001, basis_guard_bps=200.0))
    act = CarryAction("rebalance", "drift", spot_price=65000.0, perp_price=65000.0)
    res = s._rebalance(act)
    assert res["rebalanced"] is True
    ex.place_spot_order.assert_called_once()
    order = ex.place_spot_order.call_args.args[0]
    assert order["side"] == "Sell"
    # delta = 0.001 BTC, sell qty in BTC
    assert float(order["qty"]) == 0.001


def test_rebalance_skips_when_within_tolerance():
    """Mismatch below rebalance_min_btc → no order, baseline still reset."""
    ex = _mock_exchange(perp=65000.0, spot=65000.0, perp_size=0.001, spot_btc=0.001)
    s = CarryStrategy(ex, CarryConfig(rebalance_min_btc=0.001, basis_guard_bps=200.0))
    s.entry_basis_bps = 0.0
    act = CarryAction("rebalance", "drift", basis_bps=30.0,
                      spot_price=65000.0, perp_price=65000.0)
    res = s._rebalance(act)
    assert res["rebalanced"] is False
    ex.place_spot_order.assert_not_called()
    assert s.entry_basis_bps == 30.0  # baseline reset even on no-op


# ---------------- execution --------------------

def test_execute_open_places_both_legs():
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    # Disable conviction scaling here to isolate leg-placement behaviour.
    s = CarryStrategy(ex, CarryConfig(
        equity_fraction=0.5, qty_step=0.001, size_mult_min=1.0, size_mult_max=1.0))
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
    s = CarryStrategy(ex, CarryConfig(
        equity_fraction=0.5, qty_step=0.001, trade_log=log_path, exit_confirm_polls=1))
    s.run_once()
    # -0.0004 × 10 × 10000 = 40 bps > 31 bps close cost → EV-gated exit
    ex.get_funding_rate.return_value = {
        "fundingRate": "-0.0004", "markPrice": "65000", "lastPrice": "65000"
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


# ---------------- conviction sizing --------------------

def test_high_confidence_sizes_up():
    """Strong funding + low basis → full confidence → max multiplier."""
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0, equity=10000.0)
    s = CarryStrategy(ex, CarryConfig(
        equity_fraction=0.5, qty_step=0.001, strong_funding=0.0003,
        size_mult_min=0.5, size_mult_max=1.5,
    ))
    act = s.decide()
    assert act.action == "open"
    assert act.confidence == pytest.approx(1.0)
    # base qty = 10000*0.5/65000 = 0.0769; ×1.5 = 0.1154 → floored to 0.115
    assert act.qty == pytest.approx(0.115, abs=0.001)


def test_low_confidence_sizes_down():
    """Marginal funding + basis near guard → low confidence → below base."""
    ex = _mock_exchange(funding=0.00011, perp=65260.0, spot=65000.0, equity=10000.0)
    s = CarryStrategy(ex, CarryConfig(
        equity_fraction=0.5, qty_step=0.001, basis_guard_bps=50.0,
        strong_funding=0.0003, size_mult_min=0.5, size_mult_max=1.5,
    ))
    act = s.decide()
    assert act.action == "open"
    assert act.confidence < 0.05
    base = 10000 * 0.5 / 65000  # unscaled base qty
    assert act.qty < base  # scaled down


def test_max_notional_caps_scaled_size():
    """Confidence scaling can never breach the max_notional hard cap."""
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0, equity=100000.0)
    s = CarryStrategy(ex, CarryConfig(
        equity_fraction=0.5, qty_step=0.001, max_notional=70.0,
        strong_funding=0.0003, size_mult_min=0.5, size_mult_max=1.5,
    ))
    act = s.decide()
    assert act.action == "open"
    assert act.qty * 65000 <= 70.0 + 1e-6


def test_confidence_logged(tmp_path: Path):
    log_path = str(tmp_path / "trades.csv")
    ex = _mock_exchange(funding=0.0003, perp=65000.0, spot=65000.0)
    s = CarryStrategy(ex, CarryConfig(
        equity_fraction=0.5, qty_step=0.001, trade_log=log_path,
        size_mult_min=1.0, size_mult_max=1.0,
    ))
    s.run_once()
    with open(log_path) as f:
        rows = list(csv.DictReader(f))
    assert float(rows[0]["confidence"]) == pytest.approx(1.0)


# ---------------- startup reconciliation --------------------

def test_reconcile_resumes_hedged_position():
    """An existing hedged pair is resumed, not duplicated."""
    ex = _mock_exchange(perp=65000.0, spot=65000.0, perp_size=0.001, spot_btc=0.001)
    s = CarryStrategy(ex, CarryConfig())
    msg = s.reconcile()
    assert s.state == CarryState.HEDGED
    assert s.position_qty == pytest.approx(0.001)
    assert "resumed" in msg
    ex.place_order.assert_not_called()  # no duplicate open


def test_reconcile_flat_when_no_position():
    ex = _mock_exchange(perp=65000.0, spot=65000.0, perp_size=0.0, spot_btc=0.0)
    s = CarryStrategy(ex, CarryConfig())
    msg = s.reconcile()
    assert s.state == CarryState.FLAT
    assert "flat" in msg


def test_reconcile_flattens_orphaned_perp():
    """A perp short with no spot hedge (naked short) is flattened on startup."""
    ex = _mock_exchange(perp=65000.0, spot=65000.0, perp_size=0.001, spot_btc=0.0)
    s = CarryStrategy(ex, CarryConfig())
    msg = s.reconcile()
    assert s.state == CarryState.FLAT
    assert "FLATTENED" in msg
    ex.place_order.assert_called_once()  # reduce-only buy
    close_call = ex.place_order.call_args.args[0]
    assert close_call["side"] == "Buy"
    assert close_call["reduceOnly"] is True


def test_reconcile_failed_read_returns_failed():
    """If positions can't be read, reconcile returns FAILED (do not trade)."""
    ex = _mock_exchange(perp=65000.0, spot=65000.0)
    ex.get_positions.side_effect = RuntimeError("api down")
    s = CarryStrategy(ex, CarryConfig())
    msg = s.reconcile()
    assert msg.startswith("FAILED")
    assert s.state == CarryState.FLAT
