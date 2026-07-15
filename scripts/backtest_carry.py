#!/usr/bin/env python3
"""Backtest the delta-neutral funding carry strategy + parameter sweep.

Loads the funding-rate Parquet produced by ``download_funding.py`` and runs the
carry backtest across a grid of entry thresholds / holding caps, plus a
per-year regime breakdown for the best config.

Usage:
    PYTHONPATH=. python3 scripts/backtest_carry.py
    PYTHONPATH=. python3 scripts/backtest_carry.py --symbol BTCUSDT
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from backtest.carry import (
    FUNDING_EVENTS_PER_YEAR,
    CarryConfig,
    run_carry_backtest,
    run_passive_carry,
    theoretical_max_carry,
)
from utils.logger import get_logger

log = get_logger("carry_backtest")

DATA_DIR = Path("data")


def _load_funding(symbol: str) -> pd.Series:
    path = DATA_DIR / f"{symbol}_funding.parquet"
    df = pd.read_parquet(path)
    df = df.sort_values("time").reset_index(drop=True)
    s = pd.Series(df["fundingRate"].to_numpy(dtype="float64"), index=pd.DatetimeIndex(df["time"]))
    s.name = "fundingRate"
    return s


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def sweep(rates: pd.Series) -> pd.DataFrame:
    """Grid search over entry threshold and max-hold cap."""
    rows = []
    for entry in (0.0001, 0.0002, 0.0003, 0.0005, 0.0008, 0.001):
        for max_hold in (3, 5, 8, 12, 20):
            cfg = CarryConfig(entry_threshold=entry, max_hold_events=max_hold)
            res = run_carry_backtest(rates, cfg)
            rows.append(
                {
                    "entry_pct": entry * 100,
                    "max_hold": max_hold,
                    "trades": res.n_trades,
                    "win%": res.win_rate * 100,
                    "PF": res.profit_factor,
                    "return": res.total_return,
                    "cagr": res.cagr,
                    "sharpe": res.sharpe,
                    "maxDD": res.max_drawdown,
                    "in_mkt%": res.time_in_market * 100,
                }
            )
    return pd.DataFrame(rows)


def per_year(rates: pd.Series, cfg: CarryConfig) -> pd.DataFrame:
    """Run a single config on each calendar year."""
    rows = []
    for year, grp in rates.groupby(rates.index.year):
        res = run_carry_backtest(grp, cfg)
        rows.append(
            {
                "year": year,
                "events": len(grp),
                "trades": res.n_trades,
                "win%": res.win_rate * 100,
                "return": res.total_return,
                "sharpe": res.sharpe,
                "maxDD": res.max_drawdown,
                "in_mkt%": res.time_in_market * 100,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Funding carry backtest")
    ap.add_argument("--symbol", default="BTCUSDT")
    args = ap.parse_args()

    rates = _load_funding(args.symbol)
    n_years = len(rates) / FUNDING_EVENTS_PER_YEAR
    print(f"\n{'=' * 72}")
    print(f"  DELTA-NEUTRAL FUNDING CARRY  —  {args.symbol}")
    print(f"  {len(rates)} funding events | {rates.index[0].date()} → {rates.index[-1].date()}"
          f" | ~{n_years:.1f} yrs")
    print(f"  mean rate {_fmt_pct(rates.mean())}  "
          f"extreme(|>0.03%) {(rates.abs() > 0.0003).sum()} events")
    print(f"{'=' * 72}")

    # Baselines: passive carry (realistic) vs theoretical max (perfect foresight).
    passive = run_passive_carry(rates)
    tmax = theoretical_max_carry(rates)
    print("\n--- BASELINES ---")
    print(f"  Passive carry (short perp + long spot, hold 4yr): "
          f"{_fmt_pct(passive.total_return)}  |  {_fmt_pct(passive.cagr)}/yr  "
          f"| Sharpe {passive.sharpe:.2f} | maxDD {_fmt_pct(passive.max_drawdown)}")
    print(f"  Theoretical max (collect |rate|, free flips):     {_fmt_pct(tmax)}  "
          f"| {_fmt_pct(tmax / n_years)}/yr  [unreachable upper bound]")
    print(f"  → Realistic passive funding yield ≈ {_fmt_pct(passive.cagr)}/yr, "
          f"delta-neutral (no directional risk)")

    # ---- parameter sweep -------------------------------------------------
    print("\n--- PARAMETER SWEEP (entry threshold × max hold) ---")
    sw = sweep(rates)
    with pd.option_context("display.max_rows", None, "display.width", 200,
                           "display.float_format", lambda v: f"{v:,.2f}"):
        print(sw.to_string(index=False))

    best_row = sw.sort_values("sharpe", ascending=False).iloc[0]
    best_entry = float(best_row["entry_pct"]) / 100.0
    best_hold = int(best_row["max_hold"])
    print(f"\nBest Sharpe config: entry≥{best_entry*100:.2f}%  max_hold={best_hold}")

    # ---- best config detail + per-year ----------------------------------
    cfg = CarryConfig(entry_threshold=best_entry, max_hold_events=best_hold)
    res = run_carry_backtest(rates, cfg)
    print("\n--- BEST CONFIG RESULT (full sample) ---")
    print(f"  Total return : {_fmt_pct(res.total_return)}")
    print(f"  CAGR         : {_fmt_pct(res.cagr)}")
    print(f"  Trades       : {res.n_trades}")
    print(f"  Win rate     : {res.win_rate*100:.1f}%")
    print(f"  Profit factor: {res.profit_factor:.2f}")
    print(f"  Avg funding/trade (net): {_fmt_pct(res.avg_funding_collected)}")
    print(f"  Avg hold     : {res.avg_hold_events:.1f} events "
          f"(~{res.avg_hold_events*8:.0f}h)")
    print(f"  Sharpe       : {res.sharpe:.2f}")
    print(f"  Sortino      : {res.sortino:.2f}")
    print(f"  Max drawdown : {_fmt_pct(res.max_drawdown)}")
    print(f"  Time in mkt  : {res.time_in_market*100:.1f}%")

    print("\n--- PER-YEAR BREAKDOWN (best config) ---")
    py = per_year(rates, cfg)
    with pd.option_context("display.max_rows", None, "display.width", 200,
                           "display.float_format", lambda v: f"{v:,.2f}"):
        print(py.to_string(index=False))

    # ---- save ------------------------------------------------------------
    out = {
        "symbol": args.symbol,
        "events": int(len(rates)),
        "years": round(n_years, 2),
        "passive_carry_return": passive.total_return,
        "passive_carry_cagr": passive.cagr,
        "theoretical_max": tmax,
        "best": {
            "entry_threshold": best_entry,
            "max_hold_events": best_hold,
            "total_return": res.total_return,
            "cagr": res.cagr,
            "n_trades": res.n_trades,
            "win_rate": res.win_rate,
            "profit_factor": res.profit_factor,
            "sharpe": res.sharpe,
            "sortino": res.sortino,
            "max_drawdown": res.max_drawdown,
            "time_in_market": res.time_in_market,
        },
        "per_year": py.to_dict(orient="records"),
    }
    out_path = DATA_DIR / f"{args.symbol}_carry_result.json"
    out_path.write_text(json.dumps(out, indent=2, default=float))
    print(f"\nSaved -> {out_path}")


if __name__ == "__main__":
    main()
