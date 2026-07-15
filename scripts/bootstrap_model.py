#!/usr/bin/env python3
"""Bootstrap a dummy (untrained) model checkpoint so the bot can boot.

This writes a randomly-initialized model to models/price_predictor.pt so
the inference engine has something to load on first run. Replace it with
your trained checkpoint for live trading.

    python scripts/bootstrap_model.py
"""
from __future__ import annotations

import os

from ml.model import build_model


def main() -> None:
    import torch

    os.makedirs("models", exist_ok=True)
    path = os.path.join("models", "price_predictor.pt")
    model = build_model(device="cpu")
    torch.save(model.state_dict(), path)
    print(f"Wrote dummy model checkpoint -> {path}")
    print("Replace this with a trained checkpoint before live trading.")


if __name__ == "__main__":
    main()
