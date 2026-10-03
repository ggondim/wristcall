import pytest

from conftest import silence, tone
from wristcall.audio import (
    FRAME_BYTES_IN,
    ByteAligner,
    FrameAssembler,
    duration_ms,
    frame_bytes,
    pcm16_to_float32,
    pcm16_to_wav,
    split_frames,
    wav_to_pcm16,
)


def test_frame_sizes():
    assert FRAME_BYTES_IN == 640
    assert frame_bytes(24000) == 960


def test_duration_ms():
    assert duration_ms(silence(500), 16000) == 500


def test_wav_round_trip():
    pcm = tone(100)
    wav = pcm16_to_wav(pcm, 16000)
    assert wav[:4] == b"RIFF"
    back, rate = wav_to_pcm16(wav)
    assert back == pcm and rate == 16000


def test_float_conversion_range():
    x = pcm16_to_float32(tone(20, amplitude=1.0))
    assert x.dtype.name == "float32"
    assert -1.0 <= x.min() and x.max() <= 1.0


def test_split_frames_keeps_last_partial():
    assert [len(f) for f in split_frames(b"\x00" * 1500, 640)] == [640, 640, 220]


def test_frame_assembler_rechunks_arbitrary_sizes():
    fa = FrameAssembler(640)
    assert fa.push(b"\x01" * 100) == []
    out = fa.push(b"\x01" * 1300)
    assert [len(f) for f in out] == [640, 640]
    assert fa.push(b"\x01" * 520) == [b"\x01" * 640]


def test_frame_assembler_drain_pads_with_silence():
    fa = FrameAssembler(640)
    assert fa.push(b"\x01" * 700) == [b"\x01" * 640]
    assert fa.drain() == b"\x01" * 60 + b"\x00" * 580
    assert fa.drain() == b""


@pytest.mark.parametrize("size", [0, -640])
def test_frame_assembler_rejects_non_positive_size(size):
    with pytest.raises(ValueError, match="positive"):
        FrameAssembler(size)


def test_byte_aligner_carries_odd_byte():
    al = ByteAligner()
    assert al.push(b"\x01\x02\x03") == b"\x01\x02"
    assert al.push(b"\x04") == b"\x03\x04"
    assert al.push(b"") == b""
