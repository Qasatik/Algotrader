"""M1 — Dataset builder: Parquet -> feature windows + triple-barrier labels.

Loads historical candles, computes the same features used live (so train and
serve stay consistent), then applies the **triple-barrier method** to label
each window as UP / FLAT / DOWN based on which barrier price hits first within
a forward horizon. This produces higher-quality labels than naive next-candle
direction.

Output: sliding windows of shape (seq_len, n_features) and integer labels
aligned so that X[i] uses only data available *before* label[i] (no leakage).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ml.features import build_features

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# Label classes must match ml.model: 0=DOWN, 1=FLAT, 2=UP
LABEL_DOWN, LABEL_FLAT, LABEL_UP = 0, 1, 2


@dataclass
class Dataset:
    X: np.ndarray   # (N, seq_len, n_features)
    y: np.ndarray   # (N,) int labels
    times: np.ndarray  # (N,) timestamps for time-based splits
    closes: np.ndarray  # (N,) raw close price at each sample (for backtest PnL)


def load_candles(symbol: str, interval: str = "1") -> pd.DataFrame:
    """Load a Parquet file produced by scripts/download_history.py."""
    path = os.path.join(DATA_DIR, f"{symbol}_{interval}m.parquet")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} not found. Run: python scripts/download_history.py "
            f"--symbol {symbol} --interval {interval}"
        )
    df = pd.read_parquet(path)
    return df.sort_values("open_time").reset_index(drop=True)


def triple_barrier_labels(
    close: pd.Series,
    take_pct: float = 0.004,
    stop_pct: float = 0.004,
    horizon: int = 15,
) -> np.ndarray:
    """Label each bar by which barrier is touched first within `horizon` bars.

    - UP   if +take_pct hit first
    - DOWN if -stop_pct hit first
    - FLAT if neither within horizon
    """
    closes = close.to_numpy(dtype="float64")
    n = len(closes)
    labels = np.full(n, LABEL_FLAT, dtype="int64")

    up_mult = 1.0 + take_pct
    dn_mult = 1.0 - stop_pct

    for i in range(n - horizon):
        entry = closes[i]
        up_bar = entry * up_mult
        dn_bar = entry * dn_mult
        for j in range(1, horizon + 1):
            px = closes[i + j]
            if px >= up_bar:
                labels[i] = LABEL_UP
                break
            if px <= dn_bar:
                labels[i] = LABEL_DOWN
                break
    return labels


def build_dataset(
    symbol: str,
    interval: str = "1",
    seq_len: int = 128,
    horizon: int = 15,
    take_pct: float = 0.004,
    stop_pct: float = 0.004,
) -> Dataset:
    """Assemble normalized feature windows with aligned triple-barrier labels."""
    df = load_candles(symbol, interval)
    feats = build_features(df["close"].tolist(), df["volume"].tolist())
    feats_arr = feats.to_numpy(dtype="float32")
    labels = triple_barrier_labels(df["close"], take_pct, stop_pct, horizon)
    times = df["open_time"].to_numpy()
    raw_closes = df["close"].to_numpy(dtype="float64")

    X, y, t, c = [], [], [], []
    n = len(feats_arr)
    for i in range(seq_len, n - horizon):
        window = feats_arr[i - seq_len:i]
        # z-score normalize per window (same as live inference)
        mean = window.mean(axis=0, keepdims=True)
        std = window.std(axis=0, keepdims=True)
        std[std == 0] = 1.0
        X.append((window - mean) / std)
        y.append(labels[i - 1])  # label aligned to the last bar of the window
        t.append(times[i])
        c.append(raw_closes[i - 1])  # raw close at the window's last bar

    return Dataset(
        X=np.asarray(X, dtype="float32"),
        y=np.asarray(y, dtype="int64"),
        times=np.asarray(t),
        closes=np.asarray(c, dtype="float64"),
    )


def time_split(ds: Dataset, train_frac: float = 0.7, val_frac: float = 0.15):
    """Chronological split (no shuffling across time -> avoids leakage)."""
    n = len(ds.y)
    i_train = int(n * train_frac)
    i_val = int(n * (train_frac + val_frac))
    return (
        ds.X[:i_train], ds.y[:i_train],
        ds.X[i_train:i_val], ds.y[i_train:i_val],
        ds.X[i_val:], ds.y[i_val:],
    )
