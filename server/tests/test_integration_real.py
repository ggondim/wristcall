"""Real providers. Run with: WRISTCALL_INTEGRATION_CONFIG=<yaml> pytest -m integration -v"""

import os
from pathlib import Path

import httpx
import pytest

from wristcall.config import load_config
from wristcall.providers import build_provider_set

pytestmark = pytest.mark.integration
CONFIG = os.environ.get("WRISTCALL_INTEGRATION_CONFIG")


@pytest.mark.skipif(not CONFIG, reason="set WRISTCALL_INTEGRATION_CONFIG")
async def test_real_pipeline(fixtures_dir):
    cfg = load_config(Path(CONFIG))
    profile = cfg.profiles["default"]
    async with httpx.AsyncClient(timeout=120) as http:
        ps = build_provider_set(cfg, profile, http)
        text = await ps.stt.transcribe((fixtures_dir / "speech_pt_16k.wav").read_bytes(), "pt")
        assert "teste" in text.lower()
        messages = [{"role": "system", "content": profile.system_prompt}, {"role": "user", "content": text}]
        reply = "".join([piece async for piece in ps.responder.respond(messages)])
        assert reply.strip()
        audio = b"".join([chunk async for chunk in ps.tts.synthesize("Voice test.")])
        assert len(audio) > ps.tts.sample_rate
