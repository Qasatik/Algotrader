#!/usr/bin/env python3
"""Walk-forward validation across multiple market regimes.

For each rolling window we train a FRESH model on the train period and evaluate
strictly out-of-sample on the following test period. Results are aggregated
across all regimes (2022 bear, 2023 recovery, 2024-25 bull) for a robust,
honest estimate of strategy performance — not a single lucky test split.

Because training is the expensive step, each window's model predicts once and
the same predictions are then backtested across a SWEEP of confidence
thresholds, so we can see how trade count / return / Sharpe vary with selectivity.

Usage:
    python scripts/walk_forward.py --symbol BTCUSDT --interval 5 \
        --train-months 12 --test-months 6 --step-months 6 --epochs 4

Results are printed and saved to walk_forward_results.json.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter

import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from backtest.engine import BacktestConfig, run_backtest
from backtest.metrics import trade_stats
from ml.dataset import build_dataset
from ml.model import NUM_CLASSES, PricePredictor
from utils.logger import get_logger

log = get_logger("walk_forward")

# Labeling / strategy constants (kept in sync with ml.dataset defaults).
SEQ_LEN = 128
HORIZON = 15
TAKE_PCT = 0.004
STOP_PCT = 0.004
MIN_HOLD = 15  # hold a position >= label horizon bars before exit/flip

# Confidence thresholds swept on the same per-window predictions.
DEFAULT_THRESHOLDS = (0.40, 0.45, 0.50, 0.55, 0.60)


def _periods_per_year(interval: str) -> int:
    """Annualization factor for Sharpe given the candle interval (minutes)."""
    mins = int(interval) if str(interval).isdigit() else 60
    return 365 * 24 * (60 // max(mins, 1))


# ---------------------------------------------------------------------------
# Per-window model training & inference
# ---------------------------------------------------------------------------
def train_model(
    X: np.ndarray,
    y: np.ndarray,
    epochs: int,
    batch_size: int,
    lr: float,
    device: str,
) -> PricePredictor:
    """Train a fresh GRU on the given (already-sliced) arrays."""
    model = PricePredictor().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-5)

    counts = Counter(y.tolist())
    total = len(y)
    weights = [total / (NUM_CLASSES * counts.get(c, 1)) for c in range(NUM_CLASSES)]
    crit = nn.CrossEntropyLoss(
        weight=torch.tensor(weights, dtype=torch.float32).to(device)
    )

    dl = DataLoader(
        TensorDataset(torch.from_numpy(X), torch.from_numpy(y)),
        batch_size=batch_size,
        shuffle=True,
    )
    for _ in range(epochs):
        model.train()
        for xb, yb in dl:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            loss = crit(model(xb), yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
    return model


@torch.inference_mode()
def predict(model: PricePredictor, X: np.ndarray, device: str, batch: int = 2048):
    """Return (predictions, max-softmax-confidence) for each row of X."""
    model.eval()
    preds, conf = [], []
    for i in range(0, len(X), batch):
        xb = torch.from_numpy(X[i : i + batch]).to(device)
        p = torch.softmax(model(xb), dim=-1)
        preds.append(p.argmax(dim=-1).cpu().numpy())
        conf.append(p.max(dim=-1).values.cpu().numpy())
    return np.concatenate(preds), np.concatenate(conf)


# ---------------------------------------------------------------------------
# Walk-forward driver
# ---------------------------------------------------------------------------
def walk_forward(
    symbol: str,
    interval: str = "5",
    train_months: int = 12,
    test_months: int = 6,
    step_months: int = 6,
    epochs: int = 4,
    batch_size: int = 512,
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS,
) -> dict:
    device = "cuda" if torch.cuda.is_available() else "cpu"
    ppy = _periods_per_year(interval)
    cfg = BacktestConfig()

    log.info("build_dataset", symbol=symbol, interval=interval, device=device)
    ds = build_dataset(
        symbol, interval, seq_len=SEQ_LEN, horizon=HORIZON,
        take_pct=TAKE_PCT, stop_pct=STOP_PCT,
    )
    times = pd.to_datetime(ds.times).to_numpy()  # numpy datetime64 array
    start, end = times[0], times[-1]
    log.info("dataset_ready", samples=len(ds.y), start=str(start), end=str(end),
             dist=dict(Counter(ds.y.tolist())))

    # Rolling window edges (train period immediately precedes test period).
    windows = []
    cursor = start
    while True:
        train_start = cursor
        train_end = cursor + pd.DateOffset(months=train_months)
        test_start = train_end
        test_end = test_start + pd.DateOffset(months=test_months)
        if test_start >= end:
            break
        windows.append((train_start, train_end, min(test_end, end)))
        cursor = cursor + pd.DateOffset(months=step_months)
    log.info("windows_planned", count=len(windows),
             train_months=train_months, test_months=test_months, step=step_months)

    # Per-threshold accumulators (predictions are reused across thresholds).
    agg: dict[float, dict] = {
        t: {"trades": [], "cum_return": 1.0, "sharpes": [], "max_dds": []}
        for t in thresholds
    }
    per_window_rows = []

    for idx, (trs, tre, te_end) in enumerate(windows):
        tr_mask = (times >= trs) & (times < tre)
        te_mask = (times >= tre) & (times < te_end)
        if int(tr_mask.sum()) < 2000 or int(te_mask.sum()) < 200:
            log.warning("skip_window", idx=idx, train=int(tr_mask.sum()),
                        test=int(te_mask.sum()))
            continue

        model = train_model(
            ds.X[tr_mask], ds.y[tr_mask], epochs, batch_size, 1e-3, device,
        )
        preds, conf = predict(model, ds.X[te_mask], device)
        closes = ds.closes[te_mask]
        acc = float((preds == ds.y[te_mask]).mean())

        row_base = {
            "idx": idx,
            "test_period": f"{pd.Timestamp(tre).date()}..{pd.Timestamp(te_end).date()}",
            "test_bars": int(te_mask.sum()),
            "test_acc": round(acc, 4),
        }
        for t in thresholds:
            eq, trades, rep = run_backtest(
                preds, closes, cfg, periods_per_year=ppy,
                min_holding_bars=MIN_HOLD, confidence=conf, conf_threshold=t,
            )
            agg[t]["trades"].extend(float(x) for x in trades)
            agg[t]["cum_return"] *= 1.0 + rep.total_return
            agg[t]["sharpes"].append(rep.sharpe)
            agg[t]["max_dds"].append(rep.max_drawdown)
            row_base[f"trades@{t}"] = rep.n_trades
            row_base[f"ret@{t}"] = round(rep.total_return, 4)
            row_base[f"sharpe@{t}"] = round(rep.sharpe, 2)

        per_window_rows.append(row_base)
        log.info("window_done", **row_base)

    # Aggregate per-threshold summary.
    summary = []
    for t in thresholds:
        a = agg[t]
        trades_arr = np.array(a["trades"])
        wr, pf, avg = trade_stats(trades_arr)
        summary.append({
            "conf_threshold": t,
            "n_trades": int(len(trades_arr)),
            "win_rate": round(wr, 3),
            "profit_factor": round(pf, 2) if pf != float("inf") else None,
            "avg_trade": round(avg, 5),
            "compounded_return": round(a["cum_return"] - 1.0, 4),
            "avg_sharpe": round(float(np.mean(a["sharpes"])), 2) if a["sharpes"] else 0.0,
            "worst_window_dd": round(float(min(a["max_dds"])), 4) if a["max_dds"] else 0.0,
        })

    result = {
        "symbol": symbol,
        "interval": interval,
        "device": device,
        "train_months": train_months,
        "test_months": test_months,
        "step_months": step_months,
        "epochs": epochs,
        "n_windows": len(per_window_rows),
        "data_range": f"{start} .. {end}",
        "per_threshold_summary": summary,
        "per_window": per_window_rows,
    }
    return result


def _print_report(result: dict) -> None:
    print("\n" + "=" * 78)
    print(f"WALK-FORWARD VALIDATION  {result['symbol']} {result['interval']}m  "
          f"({result['n_windows']} windows, {result['data_range']})")
    print("=" * 78)
    print(f"\n{'conf':>6} {'trades':>8} {'win%':>7} {'PF':>7} "
          f"{'avgTrade':>9} {'return':>9} {'avgSharpe':>10} {'worstDD':>9}")
    print("-" * 78)
    for s in result["per_threshold_summary"]:
        pf = s["profit_factor"]
        pf_str = f"{pf:.2f}" if pf is not None else "inf"
        print(f"{s['conf_threshold']:>6.2f} {s['n_trades']:>8} "
              f"{s['win_rate']*100:>6.1f}% {pf_str:>7} "
              f"{s['avg_trade']*100:>8.3f}% {s['compounded_return']*100:>8.2f}% "
              f"{s['avg_sharpe']:>10.2f} {s['worst_window_dd']*100:>8.1f}%")
    print("\nPer-window detail (test_acc + trades@return@0.50):")
    for w in result["per_window"]:
        print(f"  [{w['idx']:>2}] {w['test_period']}  acc={w['test_acc']:.3f}  "
              f"trades={w.get('trades@0.5', 0):>4}  ret={w.get('ret@0.5', 0)*100:>7.2f}%  "
              f"sharpe={w.get('sharpe@0.5', 0):.2f}")


def main() -> None:
    p = argparse.ArgumentParser(description="Walk-forward regime validation")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", default="5")
    p.add_argument("--train-months", type=int, default=12)
    p.add_argument("--test-months", type=int, default=6)
    p.add_argument("--step-months", type=int, default=6)
    p.add_argument("--epochs", type=int, default=4)
    p.add_argument("--batch-size", type=int, default=512)
    p.add_argument("--out", default="walk_forward_results.json")
    args = p.parse_args()

    result = walk_forward(
        symbol=args.symbol,
        interval=args.interval,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        epochs=args.epochs,
        batch_size=args.batch_size,
    )
    _print_report(result)

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\nSaved -> {args.out}")


if __name__ == "__main__":
    main()
