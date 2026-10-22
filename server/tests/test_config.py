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


def test_public_only_limit():
    data = base()
    assert parse_config(data, ENV).limits.public_only is False
    data["limits"] = {"public_only": True}
    assert parse_config(data, ENV).limits.public_only is True


def test_limits_are_validated():
    data = base()
    data["limits"] = {"max_agents_per_user": 0}
    with pytest.raises(ConfigError, match="max_agents_per_user"):
        parse_config(data, ENV)
    data["limits"] = {"max_agent_per_user": 5}
    with pytest.raises(ConfigError, match="max_agent_per_user"):
        parse_config(data, ENV)


def test_max_concurrent_calls():
    data = base()
    limits = parse_config(data, ENV).limits.max_concurrent_calls
    assert (limits.conversation, limits.one_way) == (None, None)
    data["limits"] = {"max_concurrent_calls": {"conversation": 3, "one_way": 10}}
    limits = parse_config(data, ENV).limits.max_concurrent_calls
    assert (limits.conversation, limits.one_way) == (3, 10)
    data["limits"] = {"max_concurrent_calls": {"conversation": 0}}
    with pytest.raises(ConfigError, match="conversation"):
        parse_config(data, ENV)
    data["limits"] = {"max_concurrent_calls": {"other": 1}}
    with pytest.raises(ConfigError, match="other"):
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


SERVER_AUD = "https://wc.example.com"


def _with_central(**central):
    # `audience` is required (0.6.0); the tests that are not about it get the server's URL.
    return {**base(), "central_account": {"audience": [SERVER_AUD], **central}}


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
        {"issuer": "https://cloud.example.com", "audience": []},
        {"issuer": "https://cloud.example.com", "audience": "https://wc.example.com"},
        {"issuer": "https://cloud.example.com", "audience": ["ftp://wc.example.com"]},
        {"issuer": "https://cloud.example.com", "audience": ["https://wc.example.com?x=1"]},
        {"issuer": "https://cloud.example.com", "audience": ["http://wc.example.com"]},
        {"issuer": "https://cloud.example.com", "audience": ["https://wc.example.com", "http://localhost:8765"]},
    ],
)
def test_bad_central_account_is_rejected(central):
    with pytest.raises(ConfigError):
        parse_config(_with_central(**central), ENV)


def test_central_account_without_audience_says_so():
    # E5 configs (issuer = the account issuer, no audience) must not start: the server would never accept a token.
    data = {**base(), "central_account": {"issuer": "https://auth.example.com", "clients": ["a"]}}
    with pytest.raises(ConfigError, match="central_account.audience: Field required"):
        parse_config(data, ENV)


def test_central_account_audience_is_normalized():
    cfg = parse_config(
        _with_central(issuer="https://cloud.example.com", audience=["HTTPS://WC.Example.com:443/", "https://x.test/wc/"]),
        ENV,
    )
    assert cfg.central_account.audience == ["https://wc.example.com", "https://x.test/wc"]


def test_central_account_loopback_audiences_go_alone():
    cfg = parse_config(
        _with_central(issuer="https://cloud.example.com", audience=["http://localhost:8765", "http://127.0.0.1:8765"]),
        ENV,
    )
    assert cfg.central_account.audience == ["http://localhost:8765", "http://127.0.0.1:8765"]


def test_central_account_clients_are_optional():
    cfg = parse_config(_with_central(issuer="https://cloud.example.com"), ENV)
    assert cfg.central_account.clients is None


def test_central_account_issuer_is_stripped():
    cfg = parse_config(_with_central(issuer="  https://auth.example.com/ \n", clients=["a"]), ENV)
    assert cfg.central_account.issuer == "https://auth.example.com"


@pytest.mark.parametrize(
    ("issuer", "canonical"),
    [
        ("https://Cloud.example.com:443/", "https://cloud.example.com"),
        ("HTTPS://CLOUD.EXAMPLE.COM", "https://cloud.example.com"),
        ("https://cloud.example.com:8443/wc/", "https://cloud.example.com:8443/wc"),
        ("http://LOCALHOST:80/", "http://localhost"),
    ],
)
def test_central_account_issuer_is_canonical(issuer, canonical):
    # The Cloud signs with its canonical URL as `iss`: the configured issuer must be that same form.
    cfg = parse_config(_with_central(issuer=issuer), ENV)
    assert cfg.central_account.issuer == canonical


@pytest.mark.parametrize("issuer", ["http://cloud.example.com", "http://127.0.0.2:8080", "http://[::1]:8080"])
def test_central_account_issuer_http_only_for_localhost(issuer):
    with pytest.raises(ConfigError, match="issuer"):
        parse_config(_with_central(issuer=issuer), ENV)


def test_central_account_invalid_issuer_names_the_issuer():
    with pytest.raises(ConfigError, match="issuer has an invalid host"):
        parse_config(_with_central(issuer="https://bad_host.example.com"), ENV)


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


def _with_push(**push):
    return {**base(), "push": push}


def test_push_is_optional():
    assert fake_config().push is None


def test_push_parses_and_normalizes_relay_url():
    cfg = parse_config(_with_push(relay_url="https://cloud.example.com/"), ENV)
    assert cfg.push.relay_url == "https://cloud.example.com"
    assert cfg.push.timeout_s == 10.0
    assert parse_config(_with_push(relay_url="http://localhost:8800"), ENV).push.relay_url == "http://localhost:8800"


@pytest.mark.parametrize(
    "push",
    [
        {"relay_url": "http://cloud.example.com"},
        {"relay_url": "ftp://cloud.example.com"},
        {"relay_url": "https://cloud.example.com?x=1"},
        {"relay_url": "https://cloud.example.com#frag"},
        {"relay_url": "https://user:pass@cloud.example.com"},
        {"relay_url": "https:///path"},
        {"relay_url": "https://cloud.example.com", "timeout_s": 0},
        {"relay_url": "https://cloud.example.com", "timeout_s": 31},
        {"relay_url": "https://cloud.example.com", "extra": 1},
        {},
    ],
)
def test_bad_push_is_rejected(push):
    with pytest.raises(ConfigError):
        parse_config(_with_push(**push), ENV)


# ---------- server.mode: cloud ----------

NODE_SECRET = "wc_node_" + "A1b2C3d4" * 5 + "xyz"  # prefix + 43 characters
HISTORY_KEY = "A" * 43
SEAL_KEY = "B" * 43
CLOUD_URL = "https://cloud.example.test"
assert len(NODE_SECRET) == len("wc_node_") + 43


def cloud_yaml(**over):
    """The smallest valid cloud-mode config; `over` replaces top-level sections (None removes one)."""
    data = {
        "server": {"public_url": "https://node.example.test", "mode": "cloud"},
        "providers": {
            "stt1": {"type": "openai_stt", "base_url": "https://stt.example.test/v1", "model": "w"},
            "tts1": {"type": "openai_tts", "base_url": "https://tts.example.test/v1", "model": "t", "voice": "alloy"},
        },
        "central_account": {"issuer": CLOUD_URL, "audience": ["https://node.example.test"]},
        "history": {"encryption_key": HISTORY_KEY},
        "cloud_node": {"cloud_url": CLOUD_URL, "node_id": "node-1", "node_secret": NODE_SECRET, "seal_key": SEAL_KEY},
    }
    for key, value in over.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def test_mode_defaults_to_self_hosted():
    cfg = parse_config(base(), ENV)
    assert cfg.server.mode == "self-hosted"
    assert cfg.is_cloud is False
    assert cfg.cloud_node is None
    assert cfg.limits.public_only is False
    assert cfg.limits.history_only_one_way is False
    assert cfg.limits.max_conversation_s is None
    assert cfg.limits.max_concurrent_calls.conversation is None and cfg.limits.max_concurrent_calls.one_way is None


def test_cloud_mode_parses_the_node_block():
    cfg = parse_config(cloud_yaml(), {})
    assert cfg.is_cloud is True
    node = cfg.cloud_node
    assert node.cloud_url == CLOUD_URL and node.node_id == "node-1" and node.heartbeat_s == 15.0
    assert node.node_secret.get_secret_value() == NODE_SECRET
    assert node.seal() == parse_config(cloud_yaml(), {}).cloud_node.seal() and len(node.seal()) == 32
    assert NODE_SECRET not in repr(cfg) and SEAL_KEY not in repr(cfg)


def test_cloud_mode_forces_limits():
    cfg = parse_config(cloud_yaml(), {})
    assert cfg.limits.public_only is True
    assert cfg.limits.history_only_one_way is True
    assert cfg.limits.max_concurrent_calls.conversation == 2
    assert cfg.limits.max_concurrent_calls.one_way == 2
    assert cfg.limits.max_conversation_s == 3600
    cfg = parse_config(
        cloud_yaml(limits={"max_concurrent_calls": {"conversation": 5}, "max_conversation_s": 600, "public_only": False}),
        {},
    )
    assert cfg.limits.public_only is True  # not up to the operator
    assert (cfg.limits.max_concurrent_calls.conversation, cfg.limits.max_concurrent_calls.one_way) == (5, 2)
    assert cfg.limits.max_conversation_s == 600


def test_max_conversation_s_applies_in_both_modes_and_is_bounded():
    data = base()
    data["limits"] = {"max_conversation_s": 900}
    assert parse_config(data, ENV).limits.max_conversation_s == 900
    for bad in (59, 14_401):
        data["limits"] = {"max_conversation_s": bad}
        with pytest.raises(ConfigError, match="max_conversation_s"):
            parse_config(data, ENV)


def _mut(path, value):
    def build():
        data = cloud_yaml()
        node = data
        *head, last = path
        for key in head:
            node = node[key]
        if value is None:
            del node[last]
        else:
            node[last] = value
        return data

    return build


def _with_providers(**providers):
    return lambda: cloud_yaml(providers=providers)


REFUSED = {
    "no cloud_node": lambda: cloud_yaml(cloud_node=None),
    "no central_account": lambda: cloud_yaml(central_account=None),
    "audience without public_url": _mut(["central_account", "audience"], ["https://other.example.test"]),
    "audience with extra url": _mut(["central_account", "audience"], ["https://node.example.test", "https://other.example.test"]),
    "no encryption_key": lambda: cloud_yaml(history={}),
    "openai_chat provider": _with_providers(
        stt1={"type": "openai_stt", "base_url": "https://s.example.test/v1", "model": "w"},
        llm1={"type": "openai_chat", "base_url": "https://l.example.test/v1", "model": "m"},
    ),
    "webhook provider": _with_providers(hook={"type": "webhook", "url": "https://h.example.test"}),
    "unknown provider type": _with_providers(odd={"type": "no_such_type"}),
    "push block": lambda: cloud_yaml(push={"relay_url": CLOUD_URL}),
    "short node_secret": _mut(["cloud_node", "node_secret"], "wc_node_short"),
    "node_secret wrong prefix": _mut(["cloud_node", "node_secret"], "wc_xxxx_" + "a" * 43),
    "node_secret bad chars": _mut(["cloud_node", "node_secret"], "wc_node_" + "a" * 42 + "!"),
    "seal_key equals history key": _mut(["cloud_node", "seal_key"], HISTORY_KEY),
    "seal_key not base64 key": _mut(["cloud_node", "seal_key"], "tooshort"),
    "cloud_url with user": _mut(["cloud_node", "cloud_url"], "https://user:pw@cloud.example.test"),
    "cloud_url with query": _mut(["cloud_node", "cloud_url"], "https://cloud.example.test?x=1"),
    "cloud_url with fragment": _mut(["cloud_node", "cloud_url"], "https://cloud.example.test#x"),
    "cloud_url ftp": _mut(["cloud_node", "cloud_url"], "ftp://cloud.example.test"),
    "cloud_url without host": _mut(["cloud_node", "cloud_url"], "https:///path"),
    "node_id uppercase": _mut(["cloud_node", "node_id"], "Node-1"),
    "node_id digit first": _mut(["cloud_node", "node_id"], "1node"),
    "node_id too long": _mut(["cloud_node", "node_id"], "a" * 33),
    "node_id underscore": _mut(["cloud_node", "node_id"], "node_1"),
    "heartbeat_s zero": _mut(["cloud_node", "heartbeat_s"], 0),
    "unknown cloud_node field": _mut(["cloud_node", "extra"], 1),
}


@pytest.mark.parametrize("build", REFUSED.values(), ids=REFUSED.keys())
def test_cloud_mode_refuses(build):
    with pytest.raises(ConfigError) as caught:
        parse_config(build(), {})
    message = str(caught.value)
    for secret in (NODE_SECRET, SEAL_KEY, HISTORY_KEY, "pw@"):
        assert secret not in message


def test_cloud_url_is_normalized_and_may_be_plain_http():
    data = cloud_yaml()
    data["cloud_node"]["cloud_url"] = "http://wristcall-cloud:8800/"
    assert parse_config(data, {}).cloud_node.cloud_url == "http://wristcall-cloud:8800"
    data["cloud_node"]["cloud_url"] = "https://cloud.example.test/"
    assert parse_config(data, {}).cloud_node.cloud_url == "https://cloud.example.test"


def test_audience_matches_public_url_in_canonical_form():
    data = cloud_yaml()
    data["server"]["public_url"] = "https://Node.Example.test:443/"
    assert parse_config(data, {}).is_cloud


def test_cloud_node_block_needs_cloud_mode():
    data = cloud_yaml()
    data["server"]["mode"] = "self-hosted"
    with pytest.raises(ConfigError, match="cloud_node") as caught:
        parse_config(data, {})
    assert NODE_SECRET not in str(caught.value)
    del data["server"]["mode"]
    with pytest.raises(ConfigError, match="cloud_node"):
        parse_config(data, {})


def test_unknown_mode_is_rejected():
    data = cloud_yaml()
    data["server"]["mode"] = "hybrid"
    with pytest.raises(ConfigError, match="mode"):
        parse_config(data, {})
