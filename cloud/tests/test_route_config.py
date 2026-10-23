import pytest

from wristcall_cloud.config import (
    CloudConfig,
    ConfigError,
    ProvisionSettings,
    admin_provision_settings,
    config_from_env,
    parse_client_versions,
    parse_route_regions,
    provision_settings,
)

BASE_ENV = {"WRISTCALL_CLOUD_MONGO_URL": "mongodb://localhost:27017", "WRISTCALL_CLOUD_CLIENT_IOS": "client-ios"}
REGIONS = "WRISTCALL_CLOUD_ROUTE_REGIONS"
CLIENTS = "WRISTCALL_CLOUD_CLOUD_ENTRY_CLIENTS"
LANGUAGE = "WRISTCALL_CLOUD_VOICE_NOTES_LANGUAGE"
STT = "WRISTCALL_CLOUD_VOICE_NOTES_STT"


def test_route_regions_default_and_env():
    config = CloudConfig(mongo_url="mongodb://x")
    assert config.route_regions["BR"] == ("sao", "gru")
    assert config.route_regions["*"] == ("iad",)
    assert config_from_env(BASE_ENV).route_regions == config.route_regions
    parsed = config_from_env({**BASE_ENV, REGIONS: "BR=sao;*=gru"}).route_regions
    assert parsed == {"BR": ("sao",), "*": ("gru",)}


def test_route_regions_other_defaults():
    config = CloudConfig(mongo_url="mongodb://x")
    assert config.node_fresh_s == 45
    assert config.node_stale_s == 120
    assert config.route_startup_grace_s == 60
    assert config.node_reports_window_s == 300
    assert config.node_reports_accounts == 3
    assert config.route_retry_after_s == 30
    assert config.cloud_entry_clients == {}


def test_parse_route_regions():
    assert parse_route_regions("BR=sao,gru;US=iad;*=iad") == {"BR": ("sao", "gru"), "US": ("iad",), "*": ("iad",)}
    assert parse_route_regions(" BR = sao , gru ; * = iad ; ") == {"BR": ("sao", "gru"), "*": ("iad",)}


@pytest.mark.parametrize("raw", ["BR=sao", "br=sao;*=iad", "BR=saopaulo;*=iad", "BR=;*=iad", "BRA=sao;*=iad", "*=iad;*=gru", "junk", "", "BR=SAO;*=iad"])
def test_parse_route_regions_rejects(raw):
    with pytest.raises(ValueError) as caught:
        parse_route_regions(raw)
    assert raw not in str(caught.value) or raw == ""


@pytest.mark.parametrize("raw", ["BR=zzz", "br=zzz;*=zzz", "BR=zzzzzz;*=zzz"])
def test_route_regions_env_errors_name_the_variable_not_the_value(raw):
    with pytest.raises(ConfigError) as caught:
        config_from_env({**BASE_ENV, REGIONS: raw})
    assert REGIONS in str(caught.value)
    assert raw not in str(caught.value)


def test_cloud_entry_clients_env():
    assert config_from_env(BASE_ENV).cloud_entry_clients == {}
    parsed = config_from_env({**BASE_ENV, CLIENTS: "ios=0.3.0, watchos=0.6.0"}).cloud_entry_clients
    assert parsed == {"ios": (0, 3, 0), "watchos": (0, 6, 0)}
    for bad in ("android=1.0.0", "ios=1", "ios=7.0.0,ios=8.0.0", "zzz", "ios=a.b.c"):
        with pytest.raises(ConfigError) as caught:
            config_from_env({**BASE_ENV, CLIENTS: bad})
        assert CLIENTS in str(caught.value)
        assert bad not in str(caught.value)


def test_parse_client_versions():
    assert parse_client_versions("ios=0.3.0,watchos=0.6.0") == {"ios": (0, 3, 0), "watchos": (0, 6, 0)}
    with pytest.raises(ValueError):
        parse_client_versions("android=1.0.0")


def test_voice_notes_settings():
    config = config_from_env(BASE_ENV)
    assert (config.voice_notes_language, config.voice_notes_stt) == ("en", "stt-default")
    assert config_from_env({**BASE_ENV, LANGUAGE: "pt-BR"}).voice_notes_language == "pt-BR"
    assert config_from_env({**BASE_ENV, LANGUAGE: "pt"}).voice_notes_language == "pt"
    with pytest.raises(ConfigError) as caught:
        config_from_env({**BASE_ENV, LANGUAGE: "Portuguese"})
    assert LANGUAGE in str(caught.value) and "Portuguese" not in str(caught.value)
    assert config_from_env({**BASE_ENV, STT: "my_stt-1.v2"}).voice_notes_stt == "my_stt-1.v2"
    with pytest.raises(ConfigError) as caught:
        config_from_env({**BASE_ENV, STT: "a b"})
    assert STT in str(caught.value)


def test_admin_provision_settings():
    assert admin_provision_settings({"WRISTCALL_CLOUD_MONGO_URL": "mongodb://localhost:27017"}) is None
    env = {"WRISTCALL_CLOUD_MONGO_URL": "mongodb://localhost:27017", "WRISTCALL_CLOUD_PUBLIC_URL": "https://cloud.test"}
    assert admin_provision_settings(env) == ProvisionSettings("https://cloud.test", "en", "stt-default")
    assert admin_provision_settings({**env, LANGUAGE: "pt-BR", STT: "x"}) == ProvisionSettings(
        "https://cloud.test", "pt-BR", "x"
    )
    with pytest.raises(ConfigError) as caught:
        admin_provision_settings({**env, "WRISTCALL_CLOUD_PUBLIC_URL": "http://cloud.test"})
    assert "WRISTCALL_CLOUD_PUBLIC_URL" in str(caught.value)
    with pytest.raises(ConfigError):
        admin_provision_settings({**env, "WRISTCALL_CLOUD_PUBLIC_URL": "https://cloud.test", "WRISTCALL_CLOUD_ISSUER": "https://cloud.test"})
    with pytest.raises(ConfigError):
        admin_provision_settings({**env, LANGUAGE: "Portuguese"})


def test_provision_settings_from_config():
    assert provision_settings(CloudConfig(mongo_url="mongodb://x")) is None
    config = CloudConfig(mongo_url="mongodb://x", public_url="https://cloud.test", voice_notes_language="pt")
    assert provision_settings(config) == ProvisionSettings("https://cloud.test", "pt", "stt-default")


ALERT_URL = "WRISTCALL_CLOUD_ALERT_URL"
ALERT_AFTER = "WRISTCALL_CLOUD_ALERT_AFTER_S"


def test_alert_settings(tmp_path):
    config = config_from_env(BASE_ENV)
    assert (config.alert_url, config.alert_after_s) == (None, 900)
    config = config_from_env({**BASE_ENV, ALERT_URL: " https://ntfy.test/topic-1 ", ALERT_AFTER: "600"})
    assert (config.alert_url, config.alert_after_s) == ("https://ntfy.test/topic-1", 600)
    assert "topic-1" not in repr(config)
    path = tmp_path / "alert-url"
    path.write_text("https://ntfy.test/from-file\n")
    assert config_from_env({**BASE_ENV, ALERT_URL + "_FILE": str(path)}).alert_url == "https://ntfy.test/from-file"
    with pytest.raises(ConfigError) as caught:
        config_from_env({**BASE_ENV, ALERT_URL: "https://ntfy.test/x", ALERT_URL + "_FILE": str(path)})
    assert ALERT_URL in str(caught.value)


@pytest.mark.parametrize(
    "raw",
    ["http://ntfy.test/topic-secret", "ntfy.test/topic-secret", "https:///topic-secret", "ftp://ntfy.test/x-secret"],
)
def test_alert_url_must_be_https(raw):
    with pytest.raises(ConfigError) as caught:
        config_from_env({**BASE_ENV, ALERT_URL: raw})
    assert ALERT_URL in str(caught.value) and "topic-secret" not in str(caught.value)


@pytest.mark.parametrize("raw", ["0", "-1", "ten", "1.5"])
def test_alert_after_must_be_a_positive_integer(raw):
    with pytest.raises(ConfigError) as caught:
        config_from_env({**BASE_ENV, ALERT_AFTER: raw})
    assert ALERT_AFTER in str(caught.value)
