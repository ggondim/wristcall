import pytest
from pydantic import ValidationError

from conftest import fake_config
from wristcall.config import ConfigError, Timeouts, VadConfig, load_config, parse_config


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
    p = cfg.profiles["default"]
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
    p = cfg.profiles["coach"]
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


def test_profiles_are_optional_and_limits_have_defaults():
    data = base()
    del data["profiles"]
    cfg = parse_config(data, ENV)
    assert cfg.profiles == {}
    assert (cfg.limits.max_agents_per_user, cfg.limits.max_devices_per_user, cfg.limits.custom_endpoints) == (20, 10, True)
    assert cfg.limits.custom_endpoint_types == ["openai_stt", "openai_chat", "openai_tts", "webhook"]
    for empty in ({}, None):
        data["profiles"] = empty  # the operator emptied the section after the import
        assert parse_config(data, ENV).profiles == {}


def test_limits_are_validated():
    data = base()
    data["limits"] = {"max_agents_per_user": 0}
    with pytest.raises(ConfigError, match="max_agents_per_user"):
        parse_config(data, ENV)
    data["limits"] = {"max_agent_per_user": 5}
    with pytest.raises(ConfigError, match="max_agent_per_user"):
        parse_config(data, ENV)


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


@pytest.mark.parametrize(
    "section, values",
    [
        ("vad", {"silence_ms": 0}),
        ("vad", {"silence_ms": 60_000}),
        ("vad", {"max_turn_ms": 10_000_000}),
        ("vad", {"threshold": 2}),
        ("timeouts", {"stt_s": 0}),
        ("timeouts", {"tts_s": 3600}),
    ],
)
def test_turn_and_timeout_values_are_bounded(section, values):
    model = {"vad": VadConfig, "timeouts": Timeouts}[section]
    with pytest.raises(ValidationError, match=list(values)[0]):
        model(**values)


def test_legacy_profiles_load_without_the_new_bounds():
    # Valid 0.2.0 YAML: the values are adjusted when the profile is imported as an agent (bootstrap.py).
    data = base()
    data["profiles"]["default"]["vad"] = {"silence_ms": 50, "max_turn_ms": 10_000_000}
    data["profiles"]["default"]["timeouts"] = {"first_token_s": 180}
    p = parse_config(data, ENV).profiles["default"]
    assert (p.vad.silence_ms, p.vad.max_turn_ms, p.timeouts.first_token_s) == (50, 10_000_000, 180)
    assert (p.vad.min_speech_ms, p.timeouts.stt_s) == (300, 10.0)


def test_production_values_are_within_bounds():
    data = base()
    data["profiles"]["default"]["vad"] = {"silence_ms": 2000}
    data["profiles"]["default"]["timeouts"] = {"stt_s": 30, "first_token_s": 20, "tts_s": 30}
    assert parse_config(data, ENV).profiles["default"].vad.silence_ms == 2000


def _with_central(**central):
    return {**base(), "central_account": central}


def test_central_account_is_optional():
    assert fake_config().central_account is None


def test_central_account_parses_and_normalizes_issuer():
    cfg = parse_config(_with_central(issuer="https://auth.example.com/", clients=["a", "b"]), ENV)
    assert cfg.central_account.issuer == "https://auth.example.com"
    assert cfg.central_account.clients == ["a", "b"]
    assert cfg.central_account.device_credential == "approval"


@pytest.mark.parametrize(
    "central",
    [
        {"issuer": "http://auth.example.com", "clients": ["a"]},
        {"issuer": "https://auth.example.com", "clients": []},
        {"issuer": "https://auth.example.com", "clients": ["a b"]},
        {"issuer": "https://auth.example.com", "clients": [""]},
        {"issuer": "https://auth.example.com", "clients": ["a" * 256]},
        {"issuer": "https://auth.example.com", "clients": ["a"], "device_credential": "magic"},
        {"issuer": "https://auth.example.com", "clients": ["a"], "extra": 1},
        {"issuer": "ftp://auth.example.com", "clients": ["a"]},
        {"issuer": "https://auth.example.com?x=1", "clients": ["a"]},
        {"issuer": "https://auth.example.com#frag", "clients": ["a"]},
        {"issuer": "https:///path", "clients": ["a"]},
        {"issuer": "https://user:pass@auth.example.com", "clients": ["a"]},
        {"issuer": "https://user@auth.example.com", "clients": ["a"]},
        {"issuer": "https://:pass@auth.example.com", "clients": ["a"]},
    ],
)
def test_bad_central_account_is_rejected(central):
    with pytest.raises(ConfigError):
        parse_config(_with_central(**central), ENV)


def test_central_account_issuer_is_stripped():
    cfg = parse_config(_with_central(issuer="  https://auth.example.com/ \n", clients=["a"]), ENV)
    assert cfg.central_account.issuer == "https://auth.example.com"


@pytest.mark.parametrize("issuer", ["http://localhost:8080", "http://127.0.0.1:8080"])
def test_localhost_issuer_may_use_http(issuer):
    cfg = parse_config(_with_central(issuer=issuer, clients=["a"]), ENV)
    assert cfg.central_account.issuer == issuer


def test_central_account_accepts_attestation():
    cfg = parse_config(_with_central(issuer="https://a.example", clients=["a"], device_credential="attestation"), ENV)
    assert cfg.central_account.device_credential == "attestation"


KEY = "q83vEjRWeJq83vEjRWeJq83vEjRWeJq83vEjRWeJq8w"  # 32 bytes, URL-safe base64 without padding


def test_history_defaults_keep_everything_in_the_clear():
    h = parse_config(base(), ENV).history
    assert (h.default_retention_days, h.max_retention_days, h.encryption_key, h.purge_every_s) == (None, None, None, 3600)
    assert h.key() is None


def test_history_settings_parse():
    data = base()
    data["history"] = {
        "default_retention_days": 90, "max_retention_days": 365, "encryption_key": "${HKEY}", "purge_every_s": 600,
    }
    h = parse_config(data, {**ENV, "HKEY": KEY}).history
    assert (h.default_retention_days, h.max_retention_days, h.purge_every_s) == (90, 365, 600)
    assert len(h.key()) == 32


@pytest.mark.parametrize("history", [
    {"default_retention_days": 0},
    {"max_retention_days": 30},  # a ceiling needs a default under it
    {"default_retention_days": 90, "max_retention_days": 30},
    {"purge_every_s": 10},
    {"retention": 5},
])
def test_bad_history_settings_are_rejected(history):
    data = base()
    data["history"] = history
    with pytest.raises(ConfigError):
        parse_config(data, ENV)


def test_bad_history_key_is_rejected_without_echoing_it():
    data = base()
    data["history"] = {"encryption_key": "${HKEY}"}
    with pytest.raises(ConfigError, match="32 random bytes") as exc:
        parse_config(data, {**ENV, "HKEY": "c2hvcnQta2V5"})
    assert "c2hvcnQta2V5" not in str(exc.value)


@pytest.mark.parametrize("default, ceiling, agent_days, expected", [
    (None, None, None, None),  # self-host default: kept until deleted
    (None, None, 30, 30),
    (None, None, "forever", None),
    (90, None, None, 90),  # the operator's default
    (90, None, "forever", None),
    (90, 365, "forever", 365),  # the ceiling wins over forever
    (90, 365, 1000, 365),
    (90, 365, 7, 7),
    (90, 365, None, 90),
])
def test_effective_retention(default, ceiling, agent_days, expected):
    data = base()
    data["history"] = {"default_retention_days": default, "max_retention_days": ceiling}
    assert parse_config(data, ENV).history.effective_days(agent_days) == expected
