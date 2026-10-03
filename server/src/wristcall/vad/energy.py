"""Energy based VAD (RMS in dBFS). Simple and deterministic: useful in tests and in the reference client."""

import math

import numpy as np

from ..audio import pcm16_to_float32


class EnergyVad:
    def __init__(self, threshold_dbfs: float = -45.0) -> None:
        self.threshold_dbfs = threshold_dbfs

    def is_speech(self, frame: bytes) -> bool:
        if not frame:
            return False
        x = pcm16_to_float32(frame)
        rms = float(np.sqrt(np.mean(x * x)))
        if rms <= 0.0:
            return False
        return 20 * math.log10(rms) >= self.threshold_dbfs

    def reset(self) -> None:
        pass
