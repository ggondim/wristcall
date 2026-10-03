import math
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def tone(ms: int, sample_rate: int = 16000, amplitude: float = 0.3, freq: float = 440.0) -> bytes:
    n = sample_rate * ms // 1000
    t = np.arange(n) / sample_rate
    wave = amplitude * np.sin(2 * math.pi * freq * t)
    return (wave * 32767).astype("<i2").tobytes()


def silence(ms: int, sample_rate: int = 16000) -> bytes:
    return b"\x00\x00" * (sample_rate * ms // 1000)


from wristcall.config import AppConfig, parse_config


def fake_config(**default_profile_overrides) -> AppConfig:
    profile = {
        "display_name": "Test",
        "stt": "stt",
        "responder": "llm",
        "tts": "tts",
        "system_prompt": "Be brief.",
        "vad": {"type": "energy"},
        **default_profile_overrides,
    }
    return parse_config(
        {
            "server": {"public_url": "http://testserver"},
            "providers": {
                "stt": {"type": "fake_stt", "text": "hi"},
                "llm": {"type": "echo_chat"},
                "tts": {"type": "tone_tts", "sample_rate": 16000},
            },
            "profiles": {"default": profile},
        },
        {},
    )
