"""Tests for core.pnl_tracker — net-worth snapshot, history, and P&L summary."""
from unittest.mock import MagicMock

import pytest

from core.pnl_tracker import (
    NetWorth,
    append_history,
    load_history,
    reset_baseline,
    snapshot,
    summary,
)


def _mock_exchange(equity=1000.0, btc_price=50000.0):
    ex = MagicMock()
    ex.get_total_equity.return_value = (equity, [{"coin": "USDT", "usd_value": equity}])
    ex.get_spot_price.return_value = btc_price
    return ex


# ---------------- snapshot --------------------

def test_snapshot_computes_btc_denominated_worth():
    ex = _mock_exchange(equity=1000.0, btc_price=50000.0)
    snap = snapshot(ex)
    assert snap is not None
    assert snap.equity_usdt == 1000.0
    assert snap.btc_price == 50000.0
    assert snap.equity_btc == 0.02  # 1000 / 50000


def test_snapshot_returns_none_when_no_equity():
    ex = _mock_exchange(equity=0.0, btc_price=50000.0)
    assert snapshot(ex) is None


def test_snapshot_handles_zero_btc_price():
    ex = _mock_exchange(equity=1000.0, btc_price=0.0)
    snap = snapshot(ex)
    assert snap is not None
    assert snap.equity_btc == 0.0


# ---------------- history round-trip --------------------

def test_append_and_load_history(tmp_path):
    p = str(tmp_path / "pnl.csv")
    snap = NetWorth("2026-01-01T00:00:00+00:00", 1000.0, 50000.0, 0.02)
    append_history(p, snap)
    hist = load_history(p)
    assert len(hist) == 1
    assert hist[0].equity_usdt == 1000.0
    assert hist[0].equity_btc == 0.02


def test_load_history_missing_file(tmp_path):
    assert load_history(str(tmp_path / "nope.csv")) == []


def test_load_history_skips_malformed_rows(tmp_path):
    p = tmp_path / "pnl.csv"
    p.write_text(
        "timestamp,equity_usdt,btc_price,equity_btc\n"
        "2026-01-01T00:00:00+00:00,1000.0,50000.0,0.02\n"
        "bad,row,here,\n"
        "2026-01-02T00:00:00+00:00,1010.0,50000.0,0.0202\n"
    )
    hist = load_history(str(p))
    assert len(hist) == 2  # malformed row skipped


# ---------------- summary math --------------------

def test_summary_needs_two_snapshots():
    assert summary([NetWorth("t", 1000.0, 50000.0, 0.02)])["n"] == 1


def test_summary_computes_usdt_and_btc_pnl():
    hist = [
        NetWorth("2026-01-01T00:00:00+00:00", 1000.0, 50000.0, 0.02),
        NetWorth("2026-02-01T00:00:00+00:00", 1010.0, 50000.0, 0.0202),
    ]
    s = summary(hist)
    assert s["delta_usdt"] == 10.0
    assert s["pct_usdt"] == pytest.approx(1.0)
    assert s["delta_btc"] == pytest.approx(0.0002)
    assert s["pct_btc"] == pytest.approx(1.0)


def test_summary_btc_pnl_negative_when_btc_rises_faster():
    """USDT +1% but BTC +10% => BTC-denominated worth drops ~8.2%."""
    hist = [
        NetWorth("2026-01-01T00:00:00+00:00", 1000.0, 50000.0, 0.02),
        NetWorth("2026-02-01T00:00:00+00:00", 1010.0, 55000.0, 1010.0 / 55000.0),
    ]
    s = summary(hist)
    assert s["pct_usdt"] > 0          # earned USDT funding
    assert s["pct_btc"] < 0           # but lost BTC purchasing power


def test_reset_baseline_keeps_only_last(tmp_path):
    p = str(tmp_path / "pnl.csv")
    append_history(p, NetWorth("2026-01-01T00:00:00+00:00", 1000.0, 50000.0, 0.02))
    append_history(p, NetWorth("2026-02-01T00:00:00+00:00", 1010.0, 50000.0, 0.0202))
    last = reset_baseline(p)
    assert last is not None and last.equity_usdt == 1010.0
    assert len(load_history(p)) == 1


def test_reset_baseline_empty_returns_none(tmp_path):
    assert reset_baseline(str(tmp_path / "nope.csv")) is None
