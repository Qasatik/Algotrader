#!/usr/bin/env python3
"""Train + walk-forward validate a funding-rate predictor.

Predicts the *next* 8h funding rate from lagged funding + 8h price features,
using an expanding-window walk-forward scheme (no look-ahead). Compares the ML
model against the persistence baseline (next = current) — the bar to clear.

The carry-relevant metric is whether ML improves entry/exit timing: we report
directional accuracy (sign of change) and extreme-funding recall, because the
carry strategy only acts when funding is extreme.

Usage:
    PYTHONPATH=. python3 scripts/train_funding.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_squared_error

from ml.funding_features import FEATURE_COLS, build_funding_features, persistence_baseline
from utils.logger import get_logger

log = get_logger("funding_train")

DATA_DIR = Path("data")


def _r2(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    ss_res = np.sum((y_true - y_pred) ** 2)
    ss_tot = np.sum((y_true - y_true.mean()) ** 2)
    return float(1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0


def _directional_accuracy(y_true: np.ndarray, y_now: np.ndarray, y_pred: np.ndarray) -> float:
    """Fraction of times the model correctly predicts the DIRECTION of change
    (up vs down) relative to the current funding rate."""
    actual_up = y_true > y_now
    pred_up = y_pred > y_now
    return float((actual_up == pred_up).mean())


def _extreme_recall(y_true: np.ndarray, y_pred: np.ndarray, threshold: float) -> float:
    """Of the events where next funding is extreme (>= threshold), how many did
    the model also predict as extreme? (carry entry timing)."""
    actual_extreme = y_true >= threshold
    if actual_extreme.sum() == 0:
        return 0.0
    pred_extreme = y_pred >= threshold
    return float((actual_extreme & pred_extreme).sum() / actual_extreme.sum())


def walk_forward(
    df: pd.DataFrame,
    train_months: int = 12,
    test_months: int = 6,
    step_months: int = 6,
) -> pd.DataFrame:
    """Expanding-window walk-forward. Returns df with ml_pred + persistence columns."""
    times = pd.DatetimeIndex(df["time"])
    start = times[0]
    end = times[-1]

    out = df.copy()
    out["ml_pred"] = np.nan
    out["persistence"] = persistence_baseline(df)

    cursor = start + pd.DateOffset(months=train_months)
    n_windows = 0
    while cursor < end:
        test_end = min(cursor + pd.DateOffset(months=test_months), end)
        train_mask = times < cursor
        test_mask = (times >= cursor) & (times < test_end)
        if train_mask.sum() < 100 or test_mask.sum() == 0:
            cursor += pd.DateOffset(months=step_months)
            continue

        Xtr = df.loc[train_mask, FEATURE_COLS].to_numpy(dtype="float64")
        ytr = df.loc[train_mask, "funding_next"].to_numpy(dtype="float64")
        Xte = df.loc[test_mask, FEATURE_COLS].to_numpy(dtype="float64")

        model = HistGradientBoostingRegressor(
            max_iter=300, learning_rate=0.05, max_depth=4,
            l2_regularization=1.0, random_state=42,
        )
        model.fit(Xtr, ytr)
        preds = model.predict(Xte)
        out.loc[test_mask, "ml_pred"] = preds
        n_windows += 1
        log.info("window", train_end=str(cursor.date()), test_end=str(test_end.date()),
                 n_train=int(train_mask.sum()), n_test=int(test_mask.sum()))
        cursor += pd.DateOffset(months=step_months)

    log.info("walk_forward_done", windows=n_windows)
    return out.dropna(subset=["ml_pred"]).reset_index(drop=True)


def evaluate(df: pd.DataFrame, threshold: float = 0.0003) -> dict:
    y = df["funding_next"].to_numpy(dtype="float64")
    now = df["funding_lag1"].to_numpy(dtype="float64")
    ml = df["ml_pred"].to_numpy(dtype="float64")
    pers = df["persistence"].to_numpy(dtype="float64")

    return {
        "n_events": int(len(y)),
        "ml_rmse_pct": float(np.sqrt(mean_squared_error(y, ml)) * 100),
        "pers_rmse_pct": float(np.sqrt(mean_squared_error(y, pers)) * 100),
        "ml_r2": _r2(y, ml),
        "pers_r2": _r2(y, pers),
        "ml_dir_acc": _directional_accuracy(y, now, ml),
        "pers_dir_acc": _directional_accuracy(y, now, pers),
        "ml_extreme_recall": _extreme_recall(y, ml, threshold),
        "pers_extreme_recall": _extreme_recall(y, pers, threshold),
        "threshold_pct": threshold * 100,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Funding-rate predictor (walk-forward)")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--threshold", type=float, default=0.0003,
                    help="extreme-funding threshold for recall metric (default 0.03%)")
    args = ap.parse_args()

    print("Building features...")
    df = build_funding_features(args.symbol)
    print(f"  {len(df)} events | {df['time'].iloc[0].date()} → {df['time'].iloc[-1].date()}\n")

    print("Walk-forward training (HistGradientBoosting)...")
    res = walk_forward(df)
    metrics = evaluate(res, threshold=args.threshold)

    print(f"\n{'=' * 64}")
    print(f"  FUNDING-RATE PREDICTION — ML vs PERSISTENCE ({args.symbol})")
    print(f"{'=' * 64}")
    print(f"  Events tested      : {metrics['n_events']}")
    print(f"  RMSE   ML={metrics['ml_rmse_pct']:.4f}%  "
          f"persistence={metrics['pers_rmse_pct']:.4f}%  "
          f"(lower=better)")
    print(f"  R²     ML={metrics['ml_r2']:.4f}  "
          f"persistence={metrics['pers_r2']:.4f}  (higher=better)")
    print(f"  Dir.acc ML={metrics['ml_dir_acc']*100:.1f}%  "
          f"persistence={metrics['pers_dir_acc']*100:.1f}%  "
          f"(predicts up/down correctly)")
    print(f"  Extreme recall (≥{metrics['threshold_pct']:.2f}%):  "
          f"ML={metrics['ml_extreme_recall']*100:.1f}%  "
          f"persistence={metrics['pers_extreme_recall']*100:.1f}%")
    delta_r2 = metrics["ml_r2"] - metrics["pers_r2"]
    verdict = "ML WINS" if delta_r2 > 0.01 else ("~TIE" if delta_r2 > -0.01 else "PERSISTENCE WINS")
    print(f"\n  ΔR² (ML − persistence) = {delta_r2:+.4f}  →  {verdict}")

    # Save predictions for carry integration.
    out = res[["time", "fundingRate", "funding_next", "ml_pred", "persistence"]].copy()
    out_path = DATA_DIR / f"{args.symbol}_funding_pred.parquet"
    out.to_parquet(out_path, index=False)
    print(f"\nSaved predictions -> {out_path}")

    json_path = DATA_DIR / f"{args.symbol}_funding_metrics.json"
    json_path.write_text(json.dumps(metrics, indent=2))
    print(f"Saved metrics -> {json_path}")


if __name__ == "__main__":
    main()
