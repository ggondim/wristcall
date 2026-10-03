"""Silero VAD v6 via onnxruntime, without torch. Blocks of 512 samples at 16 kHz with 64 of context."""

from functools import lru_cache
from pathlib import Path

import numpy as np
import onnxruntime as ort

from ..audio import pcm16_to_float32

MODEL_PATH = Path(__file__).with_name("silero_vad.onnx")
_SR = 16_000
_CHUNK = 512
_CONTEXT = 64


@lru_cache(maxsize=4)
def _session(path: str) -> ort.InferenceSession:
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    return ort.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])


class SileroVad:
    def __init__(self, threshold: float = 0.5, model_path: Path = MODEL_PATH) -> None:
        self._sess = _session(str(model_path))
        self.threshold = threshold
        self.neg_threshold = max(threshold - 0.15, 0.01)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _CONTEXT), dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._triggered = False

    def _infer(self, chunk: np.ndarray) -> float:
        x = np.concatenate([self._context, chunk.reshape(1, -1)], axis=1).astype(np.float32)
        out, self._state = self._sess.run(
            None, {"input": x, "state": self._state, "sr": np.array(_SR, dtype=np.int64)}
        )
        self._context = x[:, -_CONTEXT:]
        return float(out[0][0])

    def is_speech(self, frame: bytes) -> bool:
        self._pending = np.concatenate([self._pending, pcm16_to_float32(frame)])
        while len(self._pending) >= _CHUNK:
            chunk, self._pending = self._pending[:_CHUNK], self._pending[_CHUNK:]
            p = self._infer(chunk)
            if p >= self.threshold:
                self._triggered = True
            elif p < self.neg_threshold:
                self._triggered = False
        return self._triggered
