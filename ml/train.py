"""M1 — Training pipeline: train the GRU price-direction classifier.

End-to-end: build dataset -> chronological split -> train with class-weighted
cross-entropy (markets are mostly flat) -> evaluate -> save checkpoint that
ml.inference can load directly.

Usage:
    python -m ml.train --symbol BTCUSDT --interval 1 --epochs 30
"""
from __future__ import annotations

import argparse
import os
from collections import Counter

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from config.settings import get_settings
from ml.dataset import build_dataset, time_split
from ml.model import NUM_CLASSES, PricePredictor
from utils.logger import get_logger

log = get_logger("train")


def _class_weights(y: np.ndarray) -> torch.Tensor:
    """Inverse-frequency weights to fight the flat-class imbalance."""
    counts = Counter(y.tolist())
    total = len(y)
    weights = [total / (NUM_CLASSES * counts.get(c, 1)) for c in range(NUM_CLASSES)]
    return torch.tensor(weights, dtype=torch.float32)


def train(
    symbol: str,
    interval: str = "1",
    epochs: int = 30,
    batch_size: int = 256,
    lr: float = 1e-3,
    device: str | None = None,
) -> str:
    settings = get_settings()
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    log.info("train_start", symbol=symbol, interval=interval, device=device)

    ds = build_dataset(symbol=symbol, interval=interval, seq_len=settings.ml_sequence_len)
    log.info("dataset_built", samples=len(ds.y), distribution=dict(Counter(ds.y.tolist())))

    Xtr, ytr, Xva, yva, Xte, yte = time_split(ds)
    log.info("split", train=len(ytr), val=len(yva), test=len(yte))

    def loader(X, y, shuffle):
        t = TensorDataset(torch.from_numpy(X), torch.from_numpy(y))
        return DataLoader(t, batch_size=batch_size, shuffle=shuffle, drop_last=False)

    train_dl = loader(Xtr, ytr, True)
    val_dl = loader(Xva, yva, False)

    model = PricePredictor().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-5)
    criterion = nn.CrossEntropyLoss(weight=_class_weights(ytr).to(device))

    best_val_acc = 0.0
    best_state = None

    for epoch in range(1, epochs + 1):
        model.train()
        train_loss = 0.0
        for xb, yb in train_dl:
            xb, yb = xb.to(device), yb.to(device)
            optimizer.zero_grad()
            loss = criterion(model(xb), yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            train_loss += loss.item() * xb.size(0)
        train_loss /= len(ytr)

        val_acc = _evaluate(model, val_dl, device)
        log.info("epoch", epoch=epoch, train_loss=round(train_loss, 4),
                 val_acc=round(val_acc, 4))

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    # Final test report
    model.load_state_dict(best_state or model.state_dict())
    test_dl = loader(Xte, yte, False)
    test_acc = _evaluate(model, test_dl, device)
    log.info("train_done", best_val_acc=round(best_val_acc, 4), test_acc=round(test_acc, 4))

    os.makedirs(os.path.dirname(settings.ml_model_path), exist_ok=True)
    torch.save(best_state or model.state_dict(), settings.ml_model_path)
    log.info("checkpoint_saved", path=settings.ml_model_path)
    return settings.ml_model_path


@torch.inference_mode()
def _evaluate(model: PricePredictor, dl: DataLoader, device: str) -> float:
    model.eval()
    correct = total = 0
    for xb, yb in dl:
        xb, yb = xb.to(device), yb.to(device)
        pred = model(xb).argmax(dim=-1)
        correct += (pred == yb).sum().item()
        total += yb.numel()
    return correct / max(total, 1)


def main() -> None:
    p = argparse.ArgumentParser(description="Train the price-direction model")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", default="1")
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--batch-size", type=int, default=512)
    args = p.parse_args()
    train(args.symbol, args.interval, args.epochs, batch_size=args.batch_size)


if __name__ == "__main__":
    main()
