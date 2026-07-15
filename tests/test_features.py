"""Tests for feature engineering."""
import numpy as np

from ml.features import FEATURE_NAMES, build_features, to_model_input


def _synthetic_closes(n: int = 300, seed: int = 0) -> list[float]:
    rng = np.random.default_rng(seed)
    # random-walk price around 100
    rets = rng.normal(0, 0.002, size=n)
    return (100 * np.exp(np.cumsum(rets))).tolist()


def test_build_features_shape_and_columns():
    closes = _synthetic_closes()
    df = build_features(closes)
    assert list(df.columns) == FEATURE_NAMES
    assert len(df) == len(closes)
    assert not df.isna().any().any()


def test_to_model_input_shape():
    closes = _synthetic_closes()
    window = to_model_input(closes, volumes=None, seq_len=128)
    assert window is not None
    assert window.x.shape == (128, len(FEATURE_NAMES))
    assert window.x.dtype == np.float32
    # normalized -> roughly zero mean per column
    assert np.allclose(window.x.mean(axis=0), 0.0, atol=1e-5)


def test_to_model_input_none_when_insufficient():
    window = to_model_input([1, 2, 3], volumes=None, seq_len=128)
    assert window is None
