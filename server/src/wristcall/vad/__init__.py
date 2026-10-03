"""Per-frame voice detection. Timing (silence, minimum speech) lives in wristcall.turn."""

from typing import Protocol

from ..config import VadConfig


class Vad(Protocol):
    def is_speech(self, frame: bytes) -> bool: ...

    def reset(self) -> None: ...


def build_vad(cfg: VadConfig) -> Vad:
    if cfg.type == "energy":
        from .energy import EnergyVad

        return EnergyVad(cfg.energy_dbfs)
    from .silero import SileroVad

    return SileroVad(threshold=cfg.threshold)
