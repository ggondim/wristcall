import json

import pytest

from wristcall.agents import AgentSpec
from wristcall.cloud_node.seal import STAGES, SealCodec, SealError, seal_aad

KEY = bytes(range(32))
ACCOUNT, AGENT = "acc_1", "ag_1"

VOICE_NOTES = {"language": "en", "stt": {"provider": "stt-default"}, "action": None, "retention_days": None}


def custom_spec() -> dict:
    return {
        "language": "pt",
        "stt": {"type": "deepgram", "api_key": "sk-stt-secret", "headers": {"X-Token": "hdr-secret"}},
        "action": {"provider": "x"},
        "tts": None,
        "retention_days": 7,
    }


def test_stages_and_aad():
    assert STAGES == ("stt", "action", "tts")
    assert seal_aad("acc", "ag", "stt") == "acc|ag|stt"
    with pytest.raises(SealError):
        seal_aad("a|b", "ag", "stt")


def test_voice_notes_spec_opens_unsealed():
    codec = SealCodec(KEY)
    opened = codec.open_spec(VOICE_NOTES, ACCOUNT, AGENT)
    assert opened == VOICE_NOTES
    assert AgentSpec.model_validate(opened).stt.provider == "stt-default"


def test_seal_round_trip():
    codec = SealCodec(KEY)
    original = custom_spec()
    sealed = codec.seal_spec(original, ACCOUNT, AGENT)
    assert original == custom_spec()  # the input is not touched
    assert sealed["action"] == {"provider": "x"} and sealed["retention_days"] == 7 and sealed["tts"] is None
    assert sealed["language"] == "pt"
    assert list(sealed["stt"]) == ["sealed"] and sealed["stt"]["sealed"].startswith("v1.")
    text = json.dumps(sealed)
    for secret in ("sk-stt-secret", "hdr-secret", "deepgram", "X-Token"):
        assert secret not in text
    assert codec.open_spec(sealed, ACCOUNT, AGENT) == original


def test_each_seal_uses_a_fresh_nonce():
    codec = SealCodec(KEY)
    a = codec.seal_spec(custom_spec(), ACCOUNT, AGENT)["stt"]["sealed"]
    b = codec.seal_spec(custom_spec(), ACCOUNT, AGENT)["stt"]["sealed"]
    assert a != b


def test_seal_bound_to_account_agent_stage():
    codec = SealCodec(KEY)
    spec = custom_spec()
    spec["tts"] = {"type": "elevenlabs", "api_key": "k2"}
    sealed = codec.seal_spec(spec, ACCOUNT, AGENT)
    with pytest.raises(SealError):
        codec.open_spec(sealed, "acc_2", AGENT)
    with pytest.raises(SealError):
        codec.open_spec(sealed, ACCOUNT, "ag_2")
    moved = {**sealed, "stt": sealed["tts"], "tts": sealed["stt"]}
    with pytest.raises(SealError):
        codec.open_spec(moved, ACCOUNT, AGENT)
    with pytest.raises(SealError):
        SealCodec(bytes(32)).open_spec(sealed, ACCOUNT, AGENT)


def test_tampered_envelopes_are_refused():
    codec = SealCodec(KEY)
    sealed = codec.seal_spec(custom_spec(), ACCOUNT, AGENT)
    token = sealed["stt"]["sealed"]
    flipped = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
    for bad in (
        {"sealed": flipped},
        {"sealed": "v2." + token[3:]},
        {"sealed": token[3:]},
        {"sealed": "v1."},
        {"sealed": "v1.!!!"},
        {"sealed": 5},
        {"sealed": token, "extra": 1},
    ):
        with pytest.raises(SealError):
            codec.open_spec({**sealed, "stt": bad}, ACCOUNT, AGENT)


def test_clear_text_endpoint_is_refused_on_open():
    codec = SealCodec(KEY)
    for stage in STAGES:
        with pytest.raises(SealError):
            codec.open_spec({**VOICE_NOTES, stage: {"type": "x", "base_url": "https://evil.test"}}, ACCOUNT, AGENT)
    for odd in ({"provider": "a", "base_url": "https://evil.test"}, {"provider": 3}, {}, "stt-default", ["x"]):
        with pytest.raises(SealError):
            codec.open_spec({**VOICE_NOTES, "stt": odd}, ACCOUNT, AGENT)


def test_open_accepts_absent_and_null_stages():
    codec = SealCodec(KEY)
    assert codec.open_spec({"language": "en"}, ACCOUNT, AGENT) == {"language": "en"}
    assert codec.open_spec({"stt": None, "tts": {"provider": "t"}}, ACCOUNT, AGENT) == {
        "stt": None, "tts": {"provider": "t"},
    }


def test_seal_refuses_what_is_not_an_endpoint():
    codec = SealCodec(KEY)
    with pytest.raises(SealError):
        codec.seal_spec({"stt": {"base_url": "x"}}, ACCOUNT, AGENT)
    with pytest.raises(SealError):
        codec.seal_spec({"stt": {"sealed": "v1.abc"}}, ACCOUNT, AGENT)
    with pytest.raises(SealError):  # an account id with "|" would blur the fields of the AAD
        codec.seal_spec(custom_spec(), "a|b", AGENT)
    with pytest.raises(SealError):
        codec.open_spec(codec.seal_spec(custom_spec(), ACCOUNT, AGENT), "a|b", AGENT)


def test_key_must_be_32_bytes():
    with pytest.raises(ValueError):
        SealCodec(b"short")
