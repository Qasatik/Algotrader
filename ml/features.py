"""Feature engineering from raw OHLCV candles.

All features are computed with vectorized numpy/pandas operations so they
can be evaluated on the CPU cheaply before the (heavier) model runs on GPU.
The output is a fixed-length vector per timestep that the model consumes.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

FEATURE_NAMES = [
    "log_return",
    "rsi",
    "ema_fast",
    "ema_slow",
    "macd",
    "macd_signal",
    "bb_width",
    "volatility",
    "volume_z",
]


@dataclass
class FeatureWindow:
    """A normalized feature matrix ready for the model.

    Attributes:
        x: ndarray of shape (seq_len, n_features), z-score normalized.
        last_close: most recent close price (for un-normalizing signals).
    """

    x: np.ndarray
    last_close: float


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def build_features(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    """Compute the full feature dataframe from close (and optional volume) series."""
    close = pd.Series(closes, dtype="float64")
    vol = pd.Series(volumes if volumes else [1.0] * len(closes), dtype="float64")

    log_return = np.log(close / close.shift(1)).fillna(0.0)
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    macd = ema_fast - ema_slow
    macd_signal = macd.ewm(span=9, adjust=False).mean()

    # Bollinger band width
    ma = close.rolling(20, min_periods=1).mean()
    std = close.rolling(20, min_periods=1).std().fillna(0.0)
    bb_width = (2 * std) / ma.replace(0, np.nan)

    volatility = log_return.rolling(20, min_periods=1).std().fillna(0.0)
    volume_z = (vol - vol.rolling(20, min_periods=1).mean()) / vol.rolling(
        20, min_periods=1
    ).std().replace(0, np.nan)

    df = pd.DataFrame(
        {
            "log_return": log_return,
            "rsi": _rsi(close),
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "macd": macd,
            "macd_signal": macd_signal,
            "bb_width": bb_width.fillna(0.0),
            "volatility": volatility,
            "volume_z": volume_z.fillna(0.0),
        }
    ).fillna(0.0)
    return df


def to_model_input(
    closes: list[float], volumes: list[float] | None, seq_len: int
) -> FeatureWindow | None:
    """Build a normalized (seq_len, n_features) window for inference.

    Returns None if there is not enough history yet.
    """
    if len(closes) < seq_len:
        return None

    df = build_features(closes, volumes).tail(seq_len).reset_index(drop=True)
    arr = df.to_numpy(dtype="float32")

    # Z-score normalize each feature column independently (per window).
    mean = arr.mean(axis=0, keepdims=True)
    std = arr.std(axis=0, keepdims=True)
    std[std == 0] = 1.0
    x = (arr - mean) / std

    return FeatureWindow(x=x.astype("float32"), last_close=float(closes[-1]))
