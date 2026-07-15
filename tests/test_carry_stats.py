"""Tests for carry trade-log statistics aggregation (day/month/year)."""
import csv

import pytest

from core.carry_stats import load_stats


def _write_log(path: str, rows: list[dict]) -> None:
    fields = [
        "timestamp", "action", "symbol", "side", "qty", "funding_rate",
        "basis_bps", "perp_price", "spot_price", "reason",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_load_stats_day_groups_by_date(tmp_path):
    p = str(tmp_path / "trades.csv")
    _write_log(p, [
        {"timestamp": "2026-07-15T08:00:00+00:00", "action": "open",
         "qty": "0.001", "funding_rate": "0.0003", "basis_bps": "5.0"},
        {"timestamp": "2026-07-15T16:00:00+00:00", "action": "close",
         "qty": "0.001", "funding_rate": "0.0003", "basis_bps": "3.0"},
        {"timestamp": "2026-07-16T08:00:00+00:00", "action": "open",
         "qty": "0.002", "funding_rate": "0.0002", "basis_bps": "1.0"},
    ])
    stats = load_stats("day", p)
    assert len(stats) == 2
    assert stats[0].label == "2026-07-15"
    assert stats[0].opens == 1
    assert stats[0].closes == 1
    assert stats[0].avg_funding_pct == 0.03  # 0.0003 * 100
    assert stats[1].label == "2026-07-16"
    assert stats[1].opens == 1


def test_load_stats_month_groups_by_month(tmp_path):
    p = str(tmp_path / "trades.csv")
    _write_log(p, [
        {"timestamp": "2026-07-01T08:00:00+00:00", "action": "open",
         "qty": "0.001", "funding_rate": "0.0003", "basis_bps": "5.0"},
        {"timestamp": "2026-07-31T08:00:00+00:00", "action": "open",
         "qty": "0.001", "funding_rate": "0.0001", "basis_bps": "2.0"},
        {"timestamp": "2026-08-01T08:00:00+00:00", "action": "open",
         "qty": "0.001", "funding_rate": "0.0002", "basis_bps": "1.0"},
    ])
    stats = load_stats("month", p)
    assert len(stats) == 2
    assert stats[0].label == "2026-07"
    assert stats[0].opens == 2
    assert stats[0].avg_funding_pct == pytest.approx(0.02)  # avg(0.0003, 0.0001)*100
    assert stats[1].label == "2026-08"


def test_load_stats_empty_when_no_file(tmp_path):
    stats = load_stats("day", str(tmp_path / "nonexistent.csv"))
    assert stats == []


def test_load_stats_year_groups_by_year(tmp_path):
    p = str(tmp_path / "trades.csv")
    _write_log(p, [
        {"timestamp": "2026-01-01T08:00:00+00:00", "action": "open",
         "qty": "0.001", "funding_rate": "0.0003", "basis_bps": "5.0"},
        {"timestamp": "2026-12-31T08:00:00+00:00", "action": "close",
         "qty": "0.001", "funding_rate": "0.0003", "basis_bps": "3.0"},
    ])
    stats = load_stats("year", p)
    assert len(stats) == 1
    assert stats[0].label == "2026"
    assert stats[0].opens == 1
    assert stats[0].closes == 1
