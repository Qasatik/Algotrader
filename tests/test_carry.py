"""Tests for the delta-neutral funding carry backtest."""
import numpy as np
import pandas as pd
import pytest

from backtest.carry import (
    CarryConfig,
    run_carry_backtest,
    run_passive_carry,
    theoretical_max_carry,
)


def _series(rates: list[float]) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=len(rates), freq="8h", tz="UTC")
    return pd.Series(rates, index=idx, name="fundingRate")


# ---------------- CarryConfig --------------------

def test_round_trip_cost_is_double_leg():
    cfg = CarryConfig(perp_fee=0.0005, spot_fee=0.0010, slippage=0.0)
    # leg = 0.0015, round trip = entry leg + exit leg = 0.0030
    assert cfg.round_trip_cost == pytest.approx(0.0030)


# ---------------- run_carry_backtest --------------------

def test_no_trade_when_funding_below_threshold():
    """Tiny funding never triggers an entry → flat equity, zero trades."""
    rates = _series([0.00001, -0.00001, 0.00002])
    res = run_carry_backtest(rates, CarryConfig(entry_threshold=0.0003))
    assert res.n_trades == 0
    assert res.total_return == pytest.approx(0.0)


def test_correctly_positioned_collects_abs_rate():
    """Held position collects |rate| each event until funding normalises."""
    # event0: rate=0.0005 (>= threshold) → enter (no collect this event)
    # event1: rate=0.0001 (>= exit_threshold? not < 0.0001) → collect 0.0001
    # event2: rate=0.00005 (< exit 0.0001) → collect 0.00005, exit
    rates = _series([0.0005, 0.0001, 0.00005])
    cfg = CarryConfig(entry_threshold=0.0003, exit_threshold=0.0001, max_hold_events=10)
    res = run_carry_backtest(rates, cfg)
    assert res.n_trades == 1
    collected = res.trade_pnls[0] + cfg.round_trip_cost
    assert collected == pytest.approx(0.00015)


def test_no_lookahead_collection_starts_next_event():
    """Funding at the entry event must NOT be collected (no look-ahead)."""
    # event0: rate=0.001 → enter. event1: rate=0.0 → collect 0, exit (< exit thr).
    rates = _series([0.001, 0.0])
    cfg = CarryConfig(entry_threshold=0.0003, exit_threshold=0.0001, max_hold_events=10)
    res = run_carry_backtest(rates, cfg)
    assert res.n_trades == 1
    collected = res.trade_pnls[0] + cfg.round_trip_cost
    assert collected == pytest.approx(0.0)  # only event1 collected (=0)


def test_max_hold_forces_exit():
    """Position exits after max_hold_events even if funding stays extreme."""
    rates = _series([0.001, 0.001, 0.001, 0.001])  # all extreme
    cfg = CarryConfig(entry_threshold=0.0003, exit_threshold=0.0, max_hold_events=2)
    res = run_carry_backtest(rates, cfg)
    # enter at 0, collect at 1 (hold=1), collect at 2 (hold=2 → exit), re-enter at 3
    assert res.n_trades == 2


def test_negative_funding_also_collected():
    """rate < 0 → long-perp leg also collects |rate| (delta-neutral)."""
    rates = _series([-0.001, -0.001, 0.0])
    cfg = CarryConfig(entry_threshold=0.0003, exit_threshold=0.0001, max_hold_events=10)
    res = run_carry_backtest(rates, cfg)
    assert res.n_trades == 1
    # collect event1 |−0.001| = 0.001, event2 = 0 → exit; total collected 0.001
    collected = res.trade_pnls[0] + cfg.round_trip_cost
    assert collected == pytest.approx(0.001)


# ---------------- passive / theoretical --------------------

def test_passive_carry_collects_signed_funding():
    """Passive (always short perp) collects signed rate: +rate gains, -rate loses.

    Funding is applied multiplicatively (compounded), zero cost here.
    """
    r = [0.001, -0.0005, 0.002]
    rates = _series(r)
    cfg = CarryConfig(perp_fee=0.0, spot_fee=0.0, slippage=0.0)
    res = run_passive_carry(rates, cfg)
    expected = np.prod([1.0 + x for x in r]) - 1.0  # compounded product
    assert res.total_return == pytest.approx(expected)


def test_passive_carry_pays_single_round_trip():
    rates = _series([0.0, 0.0])
    cfg = CarryConfig(perp_fee=0.001, spot_fee=0.001, slippage=0.0)
    res = run_passive_carry(rates, cfg)
    # entry half + exit half applied multiplicatively, no funding
    half = cfg.round_trip_cost / 2.0
    expected = (1.0 - half) * (1.0 - half) - 1.0
    assert res.total_return == pytest.approx(expected)


def test_theoretical_max_is_abs_sum():
    rates = _series([0.001, -0.002, 0.0005])
    assert theoretical_max_carry(rates) == pytest.approx(0.0035)
