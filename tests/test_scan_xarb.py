"""Tests for the X0 cross-exchange scanner (scripts/scan_xarb.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scan_xarb import (  # noqa: E402
    _base_from_bybit,
    _base_from_okx,
    append_csv,
    scan,
    slot_ev,
)


class TestSlotEv:
    def test_short_bybit_when_bybit_positive_okx_negative(self):
        direction, ev = slot_ev(0.0005, -0.0004)
        assert direction == "short_bybit"
        assert ev == 9.0  # (0.0005 + 0.0004) * 10000

    def test_short_okx_when_bybit_negative_okx_positive(self):
        direction, ev = slot_ev(-0.0003, 0.0007)
        assert direction == "short_okx"
        assert ev == 10.0

    def test_same_sign_no_trade(self):
        assert slot_ev(0.0005, 0.0007) == ("", 0.0)
        assert slot_ev(-0.0005, -0.0007) == ("", 0.0)

    def test_zero_funding_no_trade(self):
        assert slot_ev(0.0, -0.0004) == ("", 0.0)
        assert slot_ev(0.0004, 0.0) == ("", 0.0)


class TestBaseParsing:
    def test_bybit(self):
        assert _base_from_bybit("BTCUSDT") == "BTC"
        assert _base_from_bybit("1000PEPEUSDT") == "1000PEPE"
        assert _base_from_bybit("BTCUSD") is None
        assert _base_from_bybit("USDT") is None

    def test_okx(self):
        assert _base_from_okx("BTC-USDT-SWAP") == "BTC"
        assert _base_from_okx("BTC-USD-SWAP") is None
        assert _base_from_okx("BTC-USDT") is None


class TestScan:
    def test_filters_by_min_ev_and_sorts(self):
        bybit = {"AAA": 0.0005, "BBB": 0.0002, "CCC": -0.0006, "DDD": 0.0009}
        okx = {"AAA": -0.0004, "BBB": -0.0003, "CCC": 0.0007, "DDD": 0.0010}
        rows = scan(bybit, okx, min_ev_bps=5.0)
        # DDD same sign -> excluded; BBB EV=5bps -> included; AAA=9; CCC=13
        assert [r["base"] for r in rows] == ["CCC", "AAA", "BBB"]

    def test_no_common_symbols(self):
        assert scan({"AAA": 0.001}, {"BBB": -0.001}, 1.0) == []


class TestCsv:
    def test_appends_rows_with_header_once(self, tmp_path):
        csv_path = tmp_path / "x.csv"
        rows = [
            {
                "base": "AAA",
                "direction": "short_bybit",
                "funding_bybit": 0.0005,
                "funding_okx": -0.0004,
                "ev_bps": 9.0,
            }
        ]
        append_csv(csv_path, rows)
        append_csv(csv_path, rows)
        lines = csv_path.read_text().strip().splitlines()
        assert len(lines) == 3  # header + 2 data rows
        assert lines[0].startswith("timestamp,base,direction")

    def test_no_rows_no_file(self, tmp_path):
        append_csv(tmp_path / "x.csv", [])
        assert not (tmp_path / "x.csv").exists()
