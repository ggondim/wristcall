from conftest import silence, tone
from wristcall.audio import FRAME_BYTES_IN, split_frames, wav_to_pcm16
from wristcall.config import VadConfig
from wristcall.vad import build_vad
from wristcall.vad.energy import EnergyVad
from wristcall.vad.silero import SileroVad


def frames(pcm: bytes) -> list[bytes]:
    return [f for f in split_frames(pcm, FRAME_BYTES_IN) if len(f) == FRAME_BYTES_IN]


def speech_pcm(fixtures_dir) -> bytes:
    pcm, rate = wav_to_pcm16((fixtures_dir / "speech_pt_16k.wav").read_bytes())
    assert rate == 16000
    return pcm


def test_energy_vad():
    v = EnergyVad(-45.0)
    assert not any(v.is_speech(f) for f in frames(silence(200)))
    assert all(v.is_speech(f) for f in frames(tone(200)))
    assert not v.is_speech(b"")


def test_silero_silence_is_not_speech():
    v = SileroVad()
    assert not any(v.is_speech(f) for f in frames(silence(1000)))


def test_silero_detects_speech(fixtures_dir):
    v = SileroVad()
    flags = [v.is_speech(f) for f in frames(speech_pcm(fixtures_dir))]
    assert sum(flags) / len(flags) > 0.5


def test_silero_reset_clears_trigger(fixtures_dir):
    v = SileroVad()
    for f in frames(speech_pcm(fixtures_dir)[: 16000 * 2]):
        v.is_speech(f)
    v.reset()
    assert v.is_speech(frames(silence(20))[0]) is False


def test_build_vad():
    assert isinstance(build_vad(VadConfig(type="energy")), EnergyVad)
    assert isinstance(build_vad(VadConfig()), SileroVad)
