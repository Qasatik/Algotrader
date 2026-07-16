"""Tests for the paper-trading simulation engine (P3-16)."""

from __future__ import annotations

from core.carry_strategy import CarryAction
from core.paper_trader import FUNDING_INTERVAL_S, PaperTrader

# Fixed timestamps for deterministic funding-accrual tests.
T0 = 1_000_000.0  # arbitrary epoch, floored to a funding boundary


def _open_action(qty=0.001, funding=0.0003, perp=65000.0, spot=65000.0):
    return CarryAction(
        "open", "test", qty=qty, funding_rate=funding,
        perp_price=perp, spot_price=spot, perp_side="Sell", spot_side="Buy",
    )


def _close_action():
    return CarryAction("close", "test", perp_side="Buy", spot_side="Sell")


def test_open_deducts_fees_and_creates_position():
    pt = PaperTrader(starting_equity=10000.0)
    pt.apply(_open_action(qty=0.001, perp=65000.0, spot=65000.0),
             funding_rate=0.0003, perp_price=65000.0, spot_price=65000.0, now=T0)
    assert pt.is_open
    assert pt.position.qty_btc == 0.001
    # fee = 0.001 * (65000*0.00055 + 65000*0.001) = 0.001 * 65000 * 0.00155 = 0.10075
    expected_fee = 0.001 * (65000 * 0.00055 + 65000 * 0.001)
    assert pt.total_fees == expected_fee
    assert pt.cash == 10000.0 - expected_fee
    assert pt.trade_count == 1


def test_close_realises_pnl():
    pt = PaperTrader(starting_equity=10000.0)
    pt.apply(_open_action(qty=0.001, perp=65000.0, spot=65000.0),
             funding_rate=0.0003, perp_price=65000.0, spot_price=65000.0, now=T0)
    # Close at same price → PnL ≈ 0 minus fees (delta-neutral, no price move).
    pt.apply(_close_action(), funding_rate=0.0003,
             perp_price=65000.0, spot_price=65000.0, now=T0 + 100)
    assert not pt.is_open
    # Two round-trip fees deducted.
    assert pt.trade_count == 1
    # Cash should be starting - open_fee - close_fee (no price move, no funding yet)
    expected_fees = 2 * (0.001 * (65000 * 0.00055 + 65000 * 0.001))
    assert pt.cash == 10000.0 - expected_fees


def test_funding_accrues_every_8h():
    pt = PaperTrader(starting_equity=10000.0)
    pt.apply(_open_action(qty=0.001, perp=65000.0, spot=65000.0),
             funding_rate=0.0003, perp_price=65000.0, spot_price=65000.0, now=T0)
    # No funding yet (just opened).
    assert pt.position.funding_collected == 0.0
    # Advance past one funding boundary.
    pt.apply(CarryAction("none", ""), funding_rate=0.0003,
             perp_price=65000.0, spot_price=65000.0, now=T0 + FUNDING_INTERVAL_S + 1)
    # notional = 0.001 * 65000 = 65; payment = 65 * 0.0003 = 0.0195
    assert pt.position.funding_collected == 0.0195
    # Advance past a second boundary.
    pt.apply(CarryAction("none", ""), funding_rate=0.0003,
             perp_price=65000.0, spot_price=65000.0, now=T0 + 2 * FUNDING_INTERVAL_S + 1)
    assert pt.position.funding_collected == 0.039  # 2 × 0.0195


def test_delta_neutral_pnl_cancels():
    """In a perfectly hedged position, perp gains offset spot losses."""
    pt = PaperTrader(starting_equity=10000.0)
    pt.apply(_open_action(qty=0.001, perp=65000.0, spot=65000.0),
             funding_rate=0.0, perp_price=65000.0, spot_price=65000.0, now=T0)
    # Price drops $500 on both legs.
    unrl = pt.unrealised_pnl(perp_price=64500.0, spot_price=64500.0)
    # Perp short: (65000-64500)*0.001 = +0.50
    # Spot long: (64500-65000)*0.001 = -0.50
    # Net ≈ 0 (delta-neutral). No funding (rate=0).
    assert abs(unrl) < 1e-9


def test_basis_residual_affects_pnl():
    """If perp and spot diverge (basis), the hedge isn't perfectly neutral."""
    pt = PaperTrader(starting_equity=10000.0)
    pt.apply(_open_action(qty=0.001, perp=65000.0, spot=65000.0),
             funding_rate=0.0, perp_price=65000.0, spot_price=65000.0, now=T0)
    # Perp drops to 64500, spot stays at 65000 (basis widened).
    unrl = pt.unrealised_pnl(perp_price=64500.0, spot_price=65000.0)
    # Perp short: (65000-64500)*0.001 = +0.50; spot flat: 0. Net = +0.50
    assert unrl == 0.50


def test_multiple_cycles_accumulate():
    pt = PaperTrader(starting_equity=10000.0)
    # Cycle 1: open + close at same price (lose fees).
    pt.apply(_open_action(qty=0.001, perp=65000.0, spot=65000.0),
             funding_rate=0.0003, perp_price=65000.0, spot_price=65000.0, now=T0)
    pt.apply(_close_action(), funding_rate=0.0003,
             perp_price=65000.0, spot_price=65000.0, now=T0 + 100)
    # Cycle 2.
    pt.apply(_open_action(qty=0.001, perp=66000.0, spot=66000.0),
             funding_rate=0.0003, perp_price=66000.0, spot_price=66000.0, now=T0 + 200)
    assert pt.trade_count == 2
    assert pt.is_open


def test_stats_report():
    pt = PaperTrader(starting_equity=10000.0)
    pt.apply(_open_action(qty=0.001, perp=65000.0, spot=65000.0),
             funding_rate=0.0003, perp_price=65000.0, spot_price=65000.0, now=T0)
    s = pt.stats(perp_price=65000.0, spot_price=65000.0)
    assert s.starting_equity == 10000.0
    assert s.trade_count == 1
    assert s.position_qty == 0.001
    assert s.entry_price == 65000.0
    # Equity = cash (no unrealised at same price, no funding yet).
    assert s.equity == s.cash


def test_format_stats_renders():
    pt = PaperTrader(starting_equity=10000.0)
    pt.apply(_open_action(qty=0.001, perp=65000.0, spot=65000.0),
             funding_rate=0.0003, perp_price=65000.0, spot_price=65000.0, now=T0)
    out = pt.format_stats(perp_price=65000.0, spot_price=65000.0)
    assert "Paper Trading Report" in out
    assert "10,000.00" in out
    assert "0.0010 BTC" in out
    # Flat position after close.
    pt.apply(_close_action(), funding_rate=0.0003,
             perp_price=65000.0, spot_price=65000.0, now=T0 + 100)
    out2 = pt.format_stats()
    assert "flat" in out2


def test_funding_only_when_position_open():
    """Funding should NOT accrue when flat."""
    pt = PaperTrader(starting_equity=10000.0)
    pt.apply(CarryAction("none", ""), funding_rate=0.0003,
             perp_price=65000.0, spot_price=65000.0, now=T0 + FUNDING_INTERVAL_S * 5)
    assert not pt.is_open
    assert pt.total_funding == 0.0


def test_positive_funding_is_income_for_short():
    """Positive funding rate → short perp holder RECEIVES payment (carry income)."""
    pt = PaperTrader(starting_equity=10000.0)
    pt.apply(_open_action(qty=0.001, perp=65000.0, spot=65000.0),
             funding_rate=0.0003, perp_price=65000.0, spot_price=65000.0, now=T0)
    pt.apply(CarryAction("none", ""), funding_rate=0.0003,
             perp_price=65000.0, spot_price=65000.0, now=T0 + FUNDING_INTERVAL_S + 1)
    assert pt.position.funding_collected > 0  # income, not expense
