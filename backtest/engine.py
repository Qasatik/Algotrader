"""M2 — Vectorized backtesting engine.

Simulates a long/flat/short strategy driven by per-bar predictions, applying
realistic trading costs:
  * taker fee on entry/exit,
  * slippage as a fraction of price,
  * optional funding cost for held positions.

This is intentionally simple & fast (vectorized) for quick strategy screening.
For production validation, pair it with the event-driven live path.

Usage:
    python -m backtest.engine --symbol BTCUSDT --interval 1
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd
import torch

from backtest.metrics import PerformanceReport, equity_curve, report
from config.settings import get_settings
from ml.dataset import LABEL_DOWN, LABEL_UP, build_dataset, time_split
from ml.model import PricePredictor
from utils.logger import get_logger

log = get_logger("backtest")


@dataclass
class BacktestConfig:
    fee_taker: float = 0.00055     # 0.055% taker (Bybit linear)
    fee_maker: float = -0.0001     # -0.01% maker rebate (negative = rebate)
    slippage: float = 0.0002       # 0.02% per side
    funding_per_bar: float = 0.0   # set >0 to charge funding on held pos
    position_size: float = 1.0     # fraction of equity per trade


def run_backtest(
    predictions: np.ndarray,
    closes: np.ndarray,
    cfg: BacktestConfig | None = None,
    periods_per_year: int = 365 * 24,
) -> tuple[pd.Series, np.ndarray, PerformanceReport]:
    """Vectorized backtest over aligned predictions and close prices.

    predictions: int array (0=down/short, 1=flat, 2=up/long)
    closes:      float array of close prices (same length)
    Returns (equity_curve, pnl_per_trade, report).
    """
    if cfg is None:
        cfg = BacktestConfig()
    pred = predictions.astype(int)
    px = closes.astype(float)
    n = len(pred)

    # Target position: +1 long, -1 short, 0 flat
    target = np.where(pred == LABEL_UP, 1.0,
              np.where(pred == LABEL_DOWN, -1.0, 0.0))

    # Detect position changes -> incur costs on turnover
    prev = np.concatenate([[0.0], target[:-1]])
    turnover = np.abs(target - prev)

    cost_per_bar = turnover * (cfg.fee_taker + cfg.slippage)

    # Position vector: +1 long, -1 short, 0 flat
    pos = target.copy()
    px_ret = np.zeros(n)
    px_ret[1:] = (px[1:] - px[:-1]) / px[:-1]
    # PnL = position * next-bar return
    strat_ret = pos * px_ret
    # subtract costs + funding on held position
    net_ret = strat_ret - cost_per_bar - np.abs(pos) * cfg.funding_per_bar

    eq = equity_curve(pd.Series(net_ret))

    # Per-trade PnL: sum returns between position changes
    pnl_trades = []
    in_trade = False
    entry_idx = 0
    cur_dir = 0.0
    for i in range(n):
        if target[i] != 0 and not in_trade:
            in_trade = True
            entry_idx = i
            cur_dir = target[i]
        elif in_trade and (target[i] != cur_dir or target[i] == 0):
            pnl_trades.append(float(net_ret[entry_idx:i].sum()))
            in_trade = target[i] != 0
            entry_idx = i
            cur_dir = target[i]
    if in_trade:
        pnl_trades.append(float(net_ret[entry_idx:].sum()))

    rep = report(eq, np.array(pnl_trades), periods_per_year)
    return eq, np.array(pnl_trades), rep


def backtest_oracle(symbol: str, interval: str = "1") -> PerformanceReport:
    """Oracle upper bound: uses realized triple-barrier labels as signals.

    This is *not* a realistic result (it sees the future) — it validates the
    cost model and shows the ceiling the model could approach if perfect.
    """
    ds = build_dataset(symbol=symbol, interval=interval)
    eq, trades, rep = run_backtest(ds.y, ds.closes)
    log.info("oracle_backtest", symbol=symbol,
             total_return=round(rep.total_return, 4),
             sharpe=round(rep.sharpe, 2),
             max_dd=round(rep.max_drawdown, 4),
             trades=rep.n_trades,
             win_rate=round(rep.win_rate, 3))
    return rep


@torch.inference_mode()
def backtest_trained_model(symbol: str, interval: str = "1") -> PerformanceReport:
    """Honest out-of-sample backtest using the trained model's predictions.

    Loads the checkpoint saved by ``ml.train``, runs it only over the
    chronological *test* split (data the model never saw), and simulates
    trading with realistic fees/slippage on raw close prices.
    """
    settings = get_settings()
    device = "cuda" if torch.cuda.is_available() else "cpu"

    if not os.path.exists(settings.ml_model_path):
        raise FileNotFoundError(
            f"{settings.ml_model_path} not found. Train first: "
            f"python -m ml.train --symbol {symbol} --interval {interval}"
        )

    ds = build_dataset(symbol=symbol, interval=interval)
    _, _, _, _, Xte, yte = time_split(ds)
    # closes aligned to the test split (last 15% of samples, chronologically)
    i_val = int(len(ds.y) * 0.85)
    test_closes = ds.closes[i_val:]

    model = PricePredictor().to(device)
    state = torch.load(settings.ml_model_path, map_location=device, weights_only=True)
    model.load_state_dict(state)
    model.eval()

    preds = (
        model(torch.from_numpy(Xte).to(device)).argmax(dim=-1).cpu().numpy()
    )

    eq, trades, rep = run_backtest(preds, test_closes)
    acc = float((preds == yte).mean())
    log.info("trained_model_backtest", symbol=symbol, device=device,
             test_samples=len(yte), test_acc=round(acc, 4),
             total_return=round(rep.total_return, 4),
             sharpe=round(rep.sharpe, 2),
             max_dd=round(rep.max_drawdown, 4),
             trades=rep.n_trades,
             win_rate=round(rep.win_rate, 3))
    return rep


def main() -> None:
    p = argparse.ArgumentParser(description="Run a backtest")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", default="1")
    p.add_argument(
        "--oracle", action="store_true",
        help="run the oracle (perfect-foresight) upper bound instead of the "
             "trained model",
    )
    args = p.parse_args()

    if args.oracle:
        rep = backtest_oracle(args.symbol, args.interval)
        label = "Oracle upper bound (perfect foresight)"
    else:
        rep = backtest_trained_model(args.symbol, args.interval)
        label = "Trained model (out-of-sample test split)"

    print(f"\n=== Backtest report — {label} ===")
    for f in rep.__dataclass_fields__:
        val = getattr(rep, f)
        if isinstance(val, float):
            val = round(val, 4)
        print(f"  {f:16s}: {val}")


if __name__ == "__main__":
    main()
