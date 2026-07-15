"""PyTorch model definition for short-horizon price direction prediction.

Architecture: a small GRU encoder followed by a classification head that
predicts 3 classes (DOWN / FLAT / UP) for the next candle. GRU is chosen
over LSTM for lower latency on GPU while retaining sequence modeling.

The model is intentionally compact so inference stays well under a few
milliseconds on a modern GPU, keeping the end-to-end signal->order loop fast.
"""
from __future__ import annotations

import torch
from torch import nn

from ml.features import FEATURE_NAMES

NUM_FEATURES = len(FEATURE_NAMES)
NUM_CLASSES = 3  # 0=DOWN, 1=FLAT, 2=UP

CLASS_NAMES = ["DOWN", "FLAT", "UP"]


class PricePredictor(nn.Module):
    """GRU-based sequence classifier."""

    def __init__(
        self,
        n_features: int = NUM_FEATURES,
        hidden: int = 64,
        num_classes: int = NUM_CLASSES,
        dropout: float = 0.2,
    ) -> None:
        super().__init__()
        self.gru = nn.GRU(
            input_size=n_features,
            hidden_size=hidden,
            num_layers=2,
            batch_first=True,
            dropout=dropout,
        )
        self.head = nn.Sequential(
            nn.LayerNorm(hidden),
            nn.Linear(hidden, 32),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(32, num_classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: (batch, seq_len, n_features) -> logits (batch, num_classes)."""
        out, _ = self.gru(x)
        last = out[:, -1, :]  # take final timestep
        return self.head(last)


def build_model(device: str = "cpu") -> PricePredictor:
    """Construct a fresh model on the given device."""
    model = PricePredictor()
    model.to(device)
    model.eval()
    return model


def load_model(path: str, device: str = "cpu") -> PricePredictor:
    """Load a checkpoint from disk. Falls back to a fresh model if missing."""
    import os

    model = build_model(device)
    if os.path.exists(path):
        state = torch.load(path, map_location=device, weights_only=True)
        model.load_state_dict(state)
    return model
