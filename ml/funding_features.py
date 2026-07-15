"""Feature engineering for funding-rate prediction.

Joins the 8h funding history with 5m OHLCV to build predictors for the *next*
funding rate. All features are computable at time T (only past data), so the
target ``funding_next`` (= funding[T+1]) is a clean, look-ahead-free label.

Features:
  - Lagged funding: lag1/2/3, 3-period MA, 12-period std, z-score
  - Price over the 8h window ending at the funding event:
      ret_8h, vol_8h (std of 5m returns), range_8h, volume_8h
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path("data")

FEATURE_COLS = [
    "funding_lag1", "funding_lag2", "funding_lag3",
    "funding_ma3", "funding_zscore",
    "ret_8h", "vol_8h", "range_8h", "volume_8h",
]


def _load_funding(symbol: str) -> pd.DataFrame:
    df = pd.read_parquet(DATA_DIR / f"{symbol}_funding.parquet")
    return df.sort_values("time").reset_index(drop=True)


def _price_8h_features(symbol: str) -> pd.DataFrame:
    """Aggregate 5m OHLCV into 8h windows aligned to funding timestamps.

    Uses ``closed='right', label='right'`` so the bin labeled 16:00 contains
    candles in (08:00, 16:00] — exactly the window leading up to a 16:00 funding
    event.
    """
    p = pd.read_parquet(DATA_DIR / f"{symbol}_5m.parquet")
    p = p.sort_values("open_time").copy()
    p["ret5m"] = p["close"].pct_change()
    agg = (
        p.set_index("open_time")
        .resample("8h", label="right", closed="right")
        .agg(close=("close", "last"), high=("high", "max"), low=("low", "min"),
             volume=("volume", "sum"), vol5m=("ret5m", "std"))
    )
    agg["ret_8h"] = agg["close"].pct_change()
    agg["range_8h"] = (agg["high"] - agg["low"]) / agg["close"]
    agg = agg.rename(columns={"vol5m": "vol_8h", "volume": "volume_8h"})
    # Normalise volume (raw BTC volume scales with price regime over 4 yrs).
    agg["volume_8h"] = np.log1p(agg["volume_8h"].clip(lower=0))
    return agg[["ret_8h", "vol_8h", "range_8h", "volume_8h"]]


def build_funding_features(symbol: str = "BTCUSDT") -> pd.DataFrame:
    """Return a feature DataFrame with target ``funding_next`` (look-ahead-free)."""
    fund = _load_funding(symbol)
    # Lagged / rolling funding features (all shifted → known at time T).
    fr = fund["fundingRate"]
    fund["funding_lag1"] = fr.shift(1)
    fund["funding_lag2"] = fr.shift(2)
    fund["funding_lag3"] = fr.shift(3)
    fund["funding_ma3"] = fr.shift(1).rolling(3).mean()
    std12 = fr.shift(1).rolling(12).std()
    fund["funding_zscore"] = (fund["funding_lag1"] - fund["funding_ma3"]) / (std12 + 1e-9)

    # Price features aligned to funding timestamps.
    p8 = _price_8h_features(symbol)
    fund = fund.merge(p8, left_on="time", right_index=True, how="left")

    # Target: next funding rate.
    fund["funding_next"] = fr.shift(-1)

    return fund.dropna(subset=FEATURE_COLS + ["funding_next"]).reset_index(drop=True)


def persistence_baseline(df: pd.DataFrame) -> np.ndarray:
    """Naive forecast: next funding = current funding (the model must beat this)."""
    return df["funding_lag1"].to_numpy(dtype="float64")
