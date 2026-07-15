#!/usr/bin/env python3
"""Compare carry-strategy entry signals: passive vs current-funding vs ML vs persistence.

Loads the walk-forward funding predictions and runs the timed carry backtest with
each signal (collection always uses the *actual* funding rate). Answers the key
question: does the ML funding predictor improve the carry strategy?

Usage:
    PYTHONPATH=. python3 scripts/compare_carry_signals.py
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtest.carry import CarryConfig, run_carry_backtest, run_passive_carry

DATA_DIR = Path("data")


def _fmt(x: float) -> str:
    return f"{x*100:+.2f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--entry", type=float, default=0.0003, help="entry threshold (default 0.03%)")
    args = ap.parse_args()

    pred = pd.read_parquet(DATA_DIR / f"{args.symbol}_funding_pred.parquet")
    pred = pred.sort_values("time").reset_index(drop=True)
    idx = pd.DatetimeIndex(pred["time"])
    rates = pd.Series(pred["fundingRate"].to_numpy(), index=idx, name="actual")
    ml_sig = pd.Series(pred["ml_pred"].to_numpy(), index=idx, name="ml")
    pers_sig = pd.Series(pred["persistence"].to_numpy(), index=idx, name="persistence")

    cfg = CarryConfig(entry_threshold=args.entry, exit_threshold=args.entry / 3, max_hold_events=10)
    n_years = len(rates) / (3 * 365)

    # 1) Passive (hold the whole test period)
    passive = run_passive_carry(rates, cfg)
    # 2) Timed on current funding
    cur = run_carry_backtest(rates, cfg)
    # 3) Timed on persistence (next = current)
    pers = run_carry_backtest(rates, cfg, signal=pers_sig)
    # 4) Timed on ML prediction
    ml = run_carry_backtest(rates, cfg, signal=ml_sig)

    print(f"\n{'=' * 68}")
    print(f"  CARRY SIGNAL COMPARISON — {args.symbol}  ({len(rates)} events, ~{n_years:.1f} yrs)")
    print(f"  entry≥{args.entry*100:.2f}%  exit<{(args.entry/3)*100:.2f}%  max_hold=10  "
          f"round-trip={cfg.round_trip_cost*100:.2f}%")
    print(f"{'=' * 68}")
    print(f"{'signal':<16}{'return':>10}{'/yr':>9}{'trades':>8}{'win%':>7}"
          f"{'sharpe':>8}{'maxDD':>9}{'inMkt%':>8}")
    print("-" * 68)

    for name, res in [
        ("passive(hold)", passive),
        ("current-funding", cur),
        ("persistence", pers),
        ("ML-predicted", ml),
    ]:
        print(f"{name:<16}{_fmt(res.total_return):>10}{_fmt(res.cagr):>9}"
              f"{res.n_trades:>8}{res.win_rate*100:>6.0f}%"
              f"{res.sharpe:>8.2f}{_fmt(res.max_drawdown):>9}"
              f"{res.time_in_market*100:>7.1f}%")

    best = max([("passive", passive.total_return), ("current", cur.total_return),
                ("persistence", pers.total_return), ("ML", ml.total_return)],
               key=lambda t: t[1])
    print("-" * 68)
    print(f"  Best total return: {best[0]} ({_fmt(best[1])})")
    print("\n  → If passive wins, timing (ML or otherwise) does NOT add value after costs.")


if __name__ == "__main__":
    main()
