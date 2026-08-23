"""Phase0 profitability upgrades: EV gate, funding-slot timing, maker execution,
universe auto-discovery and EV rotation ranking."""
import time
from unittest.mock import MagicMock

import pytest

from core.carry_strategy import (
    CarryAction, CarryConfig, CarryState, CarryStrategy, _fmt_step,
)
from scripts.run_carry_multi import _discover_universe, _scan_and_rank


def _mock_exchange(funding=0.0005, perp=65000.0, spot=65000.0, equity=10000.0,
                   perp_size=0.0, spot_btc=0.0, next_funding_min=None):
    ex = MagicMock()
    fr = {
        "fundingRate": str(funding),
        "markPrice": str(perp),
        "lastPrice": str(perp),
    }
    if next_funding_min is not None:
        fr["nextFundingTime"] = str(int((time.time() + next_funding_min * 60) * 1000))
    ex.get_funding_rate.return_value = fr
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
    ex.get_touch.return_value = {}
    ex.get_order_status.return_value = {}
    ex.get_instrument_info.return_value = {
        "lotSizeFilter": {"basePrecision": "0.00001", "minOrderQty": "0.00001"},
    }
    return ex


def _cfg(**kw):
    defaults = dict(
        equity_fraction=0.5, qty_step=0.001,
        size_mult_min=1.0, size_mult_max=1.0,
        spot_taker_fee=0.0,
    )
    defaults.update(kw)
    return CarryConfig(**defaults)


# ---------------- _fmt_step (lot-step formatting) --------------------

def test_fmt_step_floors_to_step():
    assert _fmt_step(0.0765432, 0.001) == "0.076"


def test_fmt_step_cleans_float_artifacts():
    # The LINKUSDT 'Qty invalid' regression: 12 * 0.1 = 1.2000000000000002
    assert _fmt_step(1.2000000000000002, 0.1) == "1.2"


def test_fmt_step_small_spot_precision():
    assert _fmt_step(0.076076, 0.00001) == "0.07607"


def test_fmt_step_unknown_step_falls_back_to_8dp():
    assert _fmt_step(0.123, 0) == "0.123"
    assert _fmt_step(0.1, -1) == "0.1"


def test_fmt_step_unit_step_no_decimals():
    assert _fmt_step(5.0, 1.0) == "5"


# ---------------- EV entry gate --------------------

def test_ev_gate_blocks_marginal_funding():
    """0.03%/8h = 3bps < 3.1bps amortized fees → EV-negative → no open.

    This is the core Phase0 fix: the old raw-funding gate happily opened at
    +0.01% where fees ate ~100% of the income.
    """
    ex = _mock_exchange(funding=0.0003)
    s = CarryStrategy(ex, _cfg())
    act = s.decide()
    assert act.action == "none"
    assert "EV" in act.reason
    assert s.state == CarryState.FLAT


def test_ev_gate_allows_strong_funding():
    ex = _mock_exchange(funding=0.0005)  # EV = 5 − 0 − 3.1 = +1.9bps
    s = CarryStrategy(ex, _cfg())
    act = s.decide()
    assert act.action == "open"
    assert "EV" in act.reason


def test_ev_gate_penalises_perp_discount():
    """Same funding but perp 20bps BELOW spot → convergence loss → blocked."""
    ex = _mock_exchange(funding=0.0005, perp=64870.0, spot=65000.0)
    s = CarryStrategy(ex, _cfg())
    act = s.decide()
    assert act.action == "none"
    assert "EV" in act.reason


def test_ev_gate_respects_custom_threshold():
    ex = _mock_exchange(funding=0.0005)  # EV +1.9bps
    s = CarryStrategy(ex, _cfg(min_ev_bps=5.0))
    assert s.decide().action == "none"
    s2 = CarryStrategy(ex, _cfg(min_ev_bps=-10.0))
    assert s2.decide().action == "open"


# ---------------- funding-slot entry timing --------------------

def test_entry_allowed_inside_window():
    ex = _mock_exchange(funding=0.0005, next_funding_min=30)  # ≤ 45min window
    s = CarryStrategy(ex, _cfg())
    assert s.decide().action == "open"


def test_entry_blocked_outside_window():
    ex = _mock_exchange(funding=0.0005, next_funding_min=120)  # 2h to slot
    s = CarryStrategy(ex, _cfg())
    act = s.decide()
    assert act.action == "none"
    assert "awaiting funding slot" in act.reason


def test_entry_blackout_right_after_slot():
    # 2 minutes after the last slot = 478min to the next → inside the 10min
    # post-slot blackout (spreads widen, funding flips at the boundary).
    ex = _mock_exchange(funding=0.0005, next_funding_min=478)
    s = CarryStrategy(ex, _cfg())
    act = s.decide()
    assert act.action == "none"
    assert "blackout" in act.reason


def test_timing_disabled_opens_anytime():
    ex = _mock_exchange(funding=0.0005, next_funding_min=120)
    s = CarryStrategy(ex, _cfg(entry_window_min=0.0, entry_blackout_min=0.0))
    assert s.decide().action == "open"


def test_missing_next_funding_time_fails_open():
    """No nextFundingTime in the ticker → timing gates must NOT block."""
    ex = _mock_exchange(funding=0.0005, next_funding_min=None)
    s = CarryStrategy(ex, _cfg())
    assert s.decide().action == "open"


# ---------------- maker execution --------------------

def _touch_mock(ex, ask="65100", bid="65090"):
    ex.get_touch.side_effect = lambda symbol, category: {
        "linear": {"ask1Price": ask, "bid1Price": bid},
        "spot": {"ask1Price": ask, "bid1Price": bid},
    }[category]


def test_maker_open_fills_both_legs_post_only():
    """Full maker fills: post-only limits at the touch, no market orders."""
    ex = _mock_exchange(funding=0.0005)
    _touch_mock(ex, ask="65100", bid="65090")
    # Both legs report instantly filled at the requested qty.
    ex.get_order_status.side_effect = (
        lambda symbol, link, category: {"orderStatus": "Filled", "cumExecQty": "0.076"}
    )
    s = CarryStrategy(ex, _cfg(maker_enabled=True, maker_poll_s=0.0))
    act = s.decide()
    assert act.qty == 0.076
    s.execute(act)

    perp = ex.place_order.call_args.args[0]
    assert perp["orderType"] == "Limit"
    assert perp["timeInForce"] == "PostOnly"
    assert perp["side"] == "Sell"
    assert perp["price"] == "65100"  # joined the ask
    assert perp["qty"] == "0.076"

    spot = ex.place_spot_order.call_args.args[0]
    assert spot["orderType"] == "Limit"
    assert spot["timeInForce"] == "PostOnly"
    assert spot["side"] == "Buy"
    assert spot["price"] == "65090"  # joined the bid
    # spot LIMIT qty is in BASE (fee=0 here → no gross-up)
    assert float(spot["qty"]) == pytest.approx(0.076, abs=1e-5)
    # no market fallback anywhere
    assert ex.place_order.call_count == 1
    assert ex.place_spot_order.call_count == 1
    assert s.state == CarryState.HEDGED


def test_maker_partial_fill_tops_up_with_market():
    """Timeout with a partial fill → cancel, then market the remainder."""
    ex = _mock_exchange(funding=0.0005)
    _touch_mock(ex)
    ex.get_order_status.return_value = {
        "orderStatus": "PartiallyFilled", "cumExecQty": "0.030",
    }
    s = CarryStrategy(ex, _cfg(
        maker_enabled=True, maker_timeout_s=0.0, maker_poll_s=0.0,
    ))
    act = s.decide()
    s.execute(act)

    # limit rested, was cancelled, remainder went market
    assert ex.cancel_order.called
    orders = [c.args[0] for c in ex.place_order.call_args_list]
    assert orders[0]["orderType"] == "Limit"
    assert any(o["orderType"] == "Market" and o["side"] == "Sell"
               for o in orders[1:])
    topup = [o for o in orders[1:] if o["orderType"] == "Market"][0]
    assert float(topup["qty"]) == pytest.approx(0.076 - 0.030, abs=1e-4)


def test_maker_rejected_falls_back_to_market():
    """A PostOnly that would cross is rejected → plain market order."""
    ex = _mock_exchange(funding=0.0005)
    _touch_mock(ex)

    def place(params):
        if params.get("orderType") == "Limit":
            raise RuntimeError("post-only would cross")
        return {"orderId": "mkt-1"}

    ex.place_order.side_effect = place
    s = CarryStrategy(ex, _cfg(
        maker_enabled=True, maker_poll_s=0.0, maker_timeout_s=0.01,
    ))
    act = s.decide()
    s.execute(act)
    orders = [c.args[0] for c in ex.place_order.call_args_list]
    assert orders[0]["orderType"] == "Limit"  # attempted first
    assert orders[-1]["orderType"] == "Market"
    assert orders[-1]["side"] == "Sell"


def test_maker_spot_buy_grosses_up_for_fee():
    """Spot LIMIT qty is grossed up by 1/(1-fee) so the NET base matches."""
    ex = _mock_exchange(funding=0.0005)
    _touch_mock(ex, ask="65100", bid="65090")

    fills = {"linear": "0.076", "spot": "0.07607607607607608"}

    def status(symbol, link, category):
        return {"orderStatus": "Filled",
                "cumExecQty": fills["spot" if category == "spot" else "linear"]}

    ex.get_order_status.side_effect = status
    s = CarryStrategy(ex, _cfg(
        maker_enabled=True, maker_poll_s=0.0, spot_taker_fee=0.001,
    ))
    act = s.decide()
    s.execute(act)
    spot = ex.place_spot_order.call_args.args[0]
    assert spot["orderType"] == "Limit"
    # gross = 0.076 / (1 - 0.001) = 0.0760760760… → floored to 0.00001 step
    assert spot["qty"] == "0.07607"


def test_basis_guard_close_bypasses_maker():
    """Emergency (basis guard) closes cross the spread with market orders."""
    ex = _mock_exchange(funding=0.0005)
    _touch_mock(ex)
    s = CarryStrategy(ex, _cfg(maker_enabled=True, maker_poll_s=0.0))
    s.state = CarryState.HEDGED
    s.position_qty = 0.076
    act = CarryAction("close", "basis guard 80bps > 50bps", qty=0.076)
    s.execute(act)
    perp = ex.place_order.call_args.args[0]
    assert perp["orderType"] == "Market"
    assert perp["reduceOnly"] is True
    assert ex.get_touch.call_count == 0  # never even looked at the book


def test_maker_disabled_uses_market():
    ex = _mock_exchange(funding=0.0005)
    _touch_mock(ex)
    s = CarryStrategy(ex, _cfg(maker_enabled=False))
    act = s.decide()
    s.execute(act)
    assert ex.place_order.call_args.args[0]["orderType"] == "Market"
    assert ex.get_touch.call_count == 0


# ---------------- universe auto-discovery --------------------

def test_discover_universe_filters_ranks_and_keeps():
    ex = MagicMock()
    ex.get_all_tickers.side_effect = lambda cat: {
        "linear": [
            {"symbol": "ETHUSDT", "turnover24h": "300000000"},
            {"symbol": "BTCUSDT", "turnover24h": "500000000"},
            {"symbol": "DUSTUSDT", "turnover24h": "100000"},   # illiquid → out
            {"symbol": "PERPONLYUSDT", "turnover24h": "9000000"},  # no spot → out
        ],
        "spot": [
            {"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"},
            {"symbol": "SPOTONLYUSDT"},  # no perp → out
        ],
    }[cat]
    uni = _discover_universe(ex, size=10, min_turnover=3_000_000.0,
                             keep=["DOGEUSDT"])
    assert uni == ["BTCUSDT", "ETHUSDT", "DOGEUSDT"]  # turnover desc + kept


def test_discover_universe_respects_size_cap():
    ex = MagicMock()
    ex.get_all_tickers.side_effect = lambda cat: {
        "linear": [
            {"symbol": "AAAUSDT", "turnover24h": "300"},
            {"symbol": "BBBUSDT", "turnover24h": "200"},
            {"symbol": "CCCUSDT", "turnover24h": "100"},
        ],
        "spot": [{"symbol": "AAAUSDT"}, {"symbol": "BBBUSDT"},
                 {"symbol": "CCCUSDT"}],
    }[cat]
    uni = _discover_universe(ex, size=2, min_turnover=0.0, keep=[])
    assert uni == ["AAAUSDT", "BBBUSDT"]


def test_discover_universe_falls_back_on_api_failure():
    ex = MagicMock()
    ex.get_all_tickers.side_effect = RuntimeError("api down")
    uni = _discover_universe(ex, size=10, min_turnover=1.0, keep=["BTCUSDT"])
    assert uni == ["BTCUSDT"]


# ---------------- EV rotation ranking --------------------

def test_scan_ranks_by_ev_not_raw_funding():
    """Same funding, but one has a −20bps perp discount → ranked lower."""
    ex = MagicMock()
    ex.get_funding_rate.side_effect = lambda sym: {
        "AAAUSDT": {"fundingRate": "0.0005", "markPrice": "100"},
        "BBBUSDT": {"fundingRate": "0.0005", "markPrice": "99.8"},  # −20bps
        "LOWUSDT": {"fundingRate": "0.00005", "markPrice": "100"},  # below min
    }[sym]
    ex.get_spot_price.return_value = 100.0
    top, ev = _scan_and_rank(ex, ["AAAUSDT", "BBBUSDT", "LOWUSDT"],
                             top_n=3, min_funding=0.0001)
    assert top[0] == "AAAUSDT"
    assert "LOWUSDT" not in top  # hard funding floor still applies
    assert ev["AAAUSDT"] == pytest.approx(5.0 - 3.1)
    assert ev["BBBUSDT"] == pytest.approx(5.0 - 20.0 - 3.1)


def test_scan_survives_symbol_errors():
    ex = MagicMock()
    ex.get_funding_rate.side_effect = lambda sym: (
        {"fundingRate": "0.0005", "markPrice": "100"} if sym == "AAAUSDT"
        else (_ for _ in ()).throw(RuntimeError("boom"))
    )
    ex.get_spot_price.return_value = 100.0
    top, ev = _scan_and_rank(ex, ["AAAUSDT", "BADUSDT"], top_n=2,
                             min_funding=0.0001)
    assert top == ["AAAUSDT"]
    assert "BADUSDT" not in ev
