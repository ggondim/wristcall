import pytest

from wristcall.config import ConfigError, load_config, parse_config


def base(**profiles_extra):
    return {
        "server": {"public_url": "https://example.test"},
        "providers": {
            "stt1": {"type": "openai_stt", "base_url": "http://stt/v1", "model": "w"},
            "llm1": {"type": "openai_chat", "base_url": "http://llm/v1", "api_key": "${KEY}", "model": "m"},
            "llm2": {"type": "openai_chat", "base_url": "http://llm2/v1", "model": "m2"},
            "tts1": {"type": "openai_tts", "base_url": "http://tts/v1", "model": "t", "voice": "alloy"},
        },
        "profiles": {
            "default": {
                "display_name": "Agent",
                "stt": "stt1",
                "responder": "llm1",
                "tts": "tts1",
                "system_prompt": "short",
                "vad": {"silence_ms": 700},
            },
            **profiles_extra,
        },
    }


ENV = {"KEY": "secret"}


def test_minimal_config_with_defaults():
    cfg = parse_config(base(), ENV)
    name, p = cfg.profile(None)
    assert name == "default"
    assert p.language == "en"
    assert p.vad.silence_ms == 700 and p.vad.min_speech_ms == 300
    assert p.timeouts.stt_s == 10.0
    assert cfg.server.pairing_approval == "code"
    assert cfg.providers["llm1"].options()["api_key"] == "secret"


def test_missing_env_var_is_error():
    with pytest.raises(ConfigError, match="KEY"):
        parse_config(base(), {})


def test_profile_inherits_and_overrides_only_declared_fields():
    cfg = parse_config(base(coach={"responder": "llm2", "system_prompt": "coach", "vad": {"min_speech_ms": 500}}), ENV)
    _, p = cfg.profile("coach")
    assert p.responder == "llm2" and p.stt == "stt1" and p.tts == "tts1"
    assert p.display_name == "Agent"
    assert p.vad.silence_ms == 700 and p.vad.min_speech_ms == 500


def test_default_profile_required():
    data = base()
    data["profiles"] = {"other": data["profiles"]["default"]}
    with pytest.raises(ConfigError, match="default"):
        parse_config(data, ENV)


def test_unknown_provider_reference():
    data = base()
    data["profiles"]["default"]["tts"] = "does_not_exist"
    with pytest.raises(ConfigError, match="does_not_exist"):
        parse_config(data, ENV)


def test_typo_in_profile_is_rejected():
    data = base()
    data["profiles"]["default"]["sytem_prompt"] = "x"
    with pytest.raises(ConfigError):
        parse_config(data, ENV)


def test_unknown_profile_lookup():
    cfg = parse_config(base(), ENV)
    with pytest.raises(KeyError):
        cfg.profile("does_not_exist")


def test_load_config_from_file(tmp_path):
    import yaml

    path = tmp_path / "wristcall.yaml"
    path.write_text(yaml.safe_dump(base()), encoding="utf-8")
    cfg = load_config(path, ENV)
    assert cfg.server.public_url == "https://example.test"


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml", ENV)


def test_validation_error_does_not_leak_interpolated_secrets():
    data = base()
    # api_key first: pydantic truncates the repr of input_value, so the order matters to reproduce the leak
    data["providers"]["llm1"] = {"api_key": "${KEY}", "base_url": "http://llm/v1", "model": "m"}
    with pytest.raises(ConfigError) as exc:
        parse_config(data, {"KEY": "SUPERSECRET"})
    assert "SUPERSECRET" not in str(exc.value)
    assert "providers.llm1.type" in str(exc.value)


def test_load_config_invalid_yaml_is_config_error(tmp_path):
    path = tmp_path / "wristcall.yaml"
    path.write_text("server: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path, ENV)


def test_non_mapping_profile_is_config_error():
    with pytest.raises(ConfigError, match="coach"):
        parse_config(base(coach="text"), ENV)


def test_non_mapping_default_profile_is_config_error():
    data = base()
    data["profiles"]["default"] = "text"
    with pytest.raises(ConfigError, match="default"):
        parse_config(data, ENV)


def test_non_mapping_nested_section_is_config_error():
    with pytest.raises(ConfigError, match="vad"):
        parse_config(base(coach={"vad": 5}), ENV)
