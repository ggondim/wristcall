import json

import pytest

from wristcall import protocol as p


def start(**over):
    msg = {"type": "session.start", "protocol": 1, "audio_in": {"codec": "pcm16", "sample_rate": 16000, "channels": 1}}
    msg.update(over)
    return json.dumps(msg)


def test_parse_session_start_without_profile():
    msg = p.parse_client_message(start())
    assert isinstance(msg, p.SessionStart)
    assert msg.profile is None and msg.audio_in.sample_rate == 16000


def test_parse_session_start_with_profile_and_unknown_fields():
    msg = p.parse_client_message(start(profile="coach", client="watch/1.0"))
    assert msg.profile == "coach"


def test_parse_mute_and_end():
    assert p.parse_client_message('{"type":"mute","muted":true}') == p.Mute(type="mute", muted=True)
    assert isinstance(p.parse_client_message('{"type":"session.end"}'), p.SessionEnd)


@pytest.mark.parametrize("text", ["not json", '{"type":"dance"}', '{"type":"mute"}', "[]"])
def test_bad_messages(text):
    with pytest.raises(p.ProtocolError) as e:
        p.parse_client_message(text)
    assert e.value.code == p.ErrorCode.BAD_MESSAGE


def test_check_session_start_protocol_version():
    msg = p.parse_client_message(start(protocol=2))
    with pytest.raises(p.ProtocolError) as e:
        p.check_session_start(msg)
    assert e.value.code == p.ErrorCode.UNSUPPORTED_PROTOCOL


def test_check_session_start_audio_format():
    msg = p.parse_client_message(start(audio_in={"codec": "pcm16", "sample_rate": 24000, "channels": 1}))
    with pytest.raises(p.ProtocolError) as e:
        p.check_session_start(msg)
    assert e.value.code == p.ErrorCode.UNSUPPORTED_AUDIO


def test_opus_codec_is_unsupported_audio():
    msg = p.parse_client_message(start(audio_in={"codec": "opus", "sample_rate": 16000, "channels": 1}))
    with pytest.raises(p.ProtocolError) as e:
        p.check_session_start(msg)
    assert e.value.code == p.ErrorCode.UNSUPPORTED_AUDIO


def test_channels_2_is_unsupported_audio():
    msg = p.parse_client_message(start(audio_in={"codec": "pcm16", "sample_rate": 16000, "channels": 2}))
    with pytest.raises(p.ProtocolError) as e:
        p.check_session_start(msg)
    assert e.value.code == p.ErrorCode.UNSUPPORTED_AUDIO


def test_protocol_checked_before_audio():
    msg = p.parse_client_message(start(protocol=2, audio_in={"codec": "opus", "sample_rate": 16000, "channels": 1}))
    with pytest.raises(p.ProtocolError) as e:
        p.check_session_start(msg)
    assert e.value.code == p.ErrorCode.UNSUPPORTED_PROTOCOL


def test_server_message_shapes():
    out = p.AudioFormat(sample_rate=24000)
    assert p.session_ready("s1", "default", "Agent", out) == {
        "type": "session.ready",
        "session_id": "s1",
        "profile": {"name": "default", "display_name": "Agent"},
        "audio_out": {"codec": "pcm16", "sample_rate": 24000, "channels": 1},
    }
    assert p.turn_user_end("limit") == {"type": "turn.user_end", "reason": "limit"}
    assert p.transcript("user", "hi") == {"type": "transcript", "role": "user", "text": "hi"}
    assert p.agent_start() == {"type": "turn.agent_start"}
    assert p.agent_end() == {"type": "turn.agent_end"}
    assert p.error("stt_failed", "x", False) == {"type": "error", "code": "stt_failed", "message": "x", "fatal": False}


def test_turn_end_defaults_to_auto():
    assert p.parse_client_message(start()).turn_end == "auto"


@pytest.mark.parametrize("mode", ["auto", "manual"])
def test_turn_end_accepts_known_modes(mode):
    assert p.parse_client_message(start(turn_end=mode)).turn_end == mode


@pytest.mark.parametrize("mode", ["push", "", None, 1, "MANUAL"])
def test_turn_end_unknown_value_is_bad_message(mode):
    with pytest.raises(p.ProtocolError) as e:
        p.parse_client_message(start(turn_end=mode))
    assert e.value.code == p.ErrorCode.BAD_MESSAGE
