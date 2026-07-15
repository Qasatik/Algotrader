"""GPU inference engine for the price-prediction model.

Responsibilities:
  * load the model checkpoint onto the configured device (GPU/CPU),
  * run low-latency inference on a feature window,
  * return a typed prediction (direction + confidence).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

import torch

from config.settings import get_settings
from ml.features import FeatureWindow, to_model_input
from ml.model import CLASS_NAMES, PricePredictor, load_model
from utils.logger import get_logger

log = get_logger("inference")


class Direction(IntEnum):
    DOWN = 0
    FLAT = 1
    UP = 2


@dataclass
class Prediction:
    direction: Direction
    confidence: float  # softmax probability of the chosen class
    probs: dict[str, float]
    last_close: float


class InferenceEngine:
    """Wraps the model + device and exposes a synchronous ``predict`` call."""

    def __init__(self) -> None:
        s = get_settings()
        self.device = s.ml_device
        self.seq_len = s.ml_sequence_len
        self.threshold = s.ml_confidence_threshold

        if torch.cuda.is_available() and self.device.startswith("cuda"):
            self._model: PricePredictor = load_model(s.ml_model_path, self.device)
            log.info(
                "model_loaded_gpu",
                device=self.device,
                gpu=torch.cuda.get_device_name(0),
            )
        else:
            # Graceful fallback to CPU if no CUDA available.
            self.device = "cpu"
            self._model = load_model(s.ml_model_path, "cpu")
            log.warning("cuda_unavailable_fallback_cpu")

    @torch.inference_mode()
    def predict(self, closes: list[float], volumes: list[float] | None = None) -> Prediction | None:
        """Run inference on the latest candle window.

        Returns None when there is not enough history to fill the window.
        """
        window: FeatureWindow | None = to_model_input(closes, volumes, self.seq_len)
        if window is None:
            return None

        x = torch.from_numpy(window.x).unsqueeze(0).to(self.device)  # (1, seq, feat)
        logits: torch.Tensor = self._model(x)
        probs = torch.softmax(logits, dim=-1).squeeze(0)
        idx = int(torch.argmax(probs).item())

        prob_map = {CLASS_NAMES[i]: float(probs[i]) for i in range(len(CLASS_NAMES))}
        return Prediction(
            direction=Direction(idx),
            confidence=float(probs[idx]),
            probs=prob_map,
            last_close=window.last_close,
        )

    def is_actionable(self, prediction: Prediction | None) -> bool:
        """A signal is actionable only if it is directional AND confident."""
        if prediction is None:
            return False
        return (
            prediction.direction != Direction.FLAT
            and prediction.confidence >= self.threshold
        )
