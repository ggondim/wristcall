import json
import re

import httpx
import respx
import yaml
from typer.testing import CliRunner

from wristcall import cli
from wristcall.pairing import Paired, PairingDenied, PairingService
from wristcall.store import open_database

runner = CliRunner()


def write_config(tmp_path, **server):
    data = {
        "server": {"public_url": "https://wc.example.test", "data_dir": str(tmp_path / "data"), **server},
        "providers": {
            "stt": {"type": "fake_stt"},
            "llm": {"type": "echo_chat"},
            "tts": {"type": "tone_tts"},
        },
        "profiles": {"default": {"display_name": "Test", "stt": "stt", "responder": "llm", "tts": "tts", "vad": {"type": "energy"}}},
    }
    path = tmp_path / "wristcall.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def code_in(output: str) -> str:
    return re.search(r"(\d{4}) (\d{4})", output).group(0).replace(" ", "")


def svc_for(tmp_path, approval="code"):
    return PairingService(open_database(tmp_path / "data"), approval)


def test_pair_without_directory_prints_url_and_valid_code(tmp_path):
    cfg = write_config(tmp_path)
    r = runner.invoke(cli.app, ["pair", "-c", str(cfg)])
    assert r.exit_code == 0, r.output
    assert "https://wc.example.test" in r.output
    assert isinstance(svc_for(tmp_path).pair(code_in(r.output), "Watch"), Paired)


@respx.mock
def test_pair_registers_in_directory(tmp_path):
    route = respx.post("https://dir.test/v1/codes").mock(return_value=httpx.Response(201, json={"code": "x", "expires_at": 0}))
    cfg = write_config(tmp_path, directory_url="https://dir.test")
    r = runner.invoke(cli.app, ["pair", "-c", str(cfg)])
    assert r.exit_code == 0, r.output
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"url": "https://wc.example.test", "code": code_in(r.output)}
    assert "Enter server URL" not in r.output


@respx.mock
def test_pair_retries_on_directory_conflict(tmp_path):
    route = respx.post("https://dir.test/v1/codes").mock(
        side_effect=[httpx.Response(409, json={"error": "conflict"}), httpx.Response(201, json={})]
    )
    cfg = write_config(tmp_path, directory_url="https://dir.test")
    r = runner.invoke(cli.app, ["pair", "-c", str(cfg)])
    assert r.exit_code == 0, r.output
    first = json.loads(route.calls[0].request.content)["code"]
    second = json.loads(route.calls[1].request.content)["code"]
    assert second == code_in(r.output)
    svc = svc_for(tmp_path)
    try:
        svc.pair(first, "x")
        raise AssertionError("the code rejected by the directory should have been discarded")
    except PairingDenied:
        pass


@respx.mock
def test_pair_with_directory_down_falls_back_to_url(tmp_path):
    respx.post("https://dir.test/v1/codes").mock(side_effect=httpx.ConnectError("down"))
    cfg = write_config(tmp_path, directory_url="https://dir.test")
    r = runner.invoke(cli.app, ["pair", "-c", str(cfg)])
    assert r.exit_code == 0
    assert "https://wc.example.test" in r.output


def test_devices_list_approve_revoke(tmp_path):
    cfg = write_config(tmp_path, pairing_approval="manual")
    svc = svc_for(tmp_path, "manual")
    pending = svc.pair(None, "My Apple Watch")
    r = runner.invoke(cli.app, ["devices", "list", "-c", str(cfg)])
    assert pending.request_id in r.output and "My Apple Watch" in r.output
    r = runner.invoke(cli.app, ["devices", "approve", pending.request_id, "-c", str(cfg)])
    assert r.exit_code == 0 and "Approved" in r.output
    paired = svc.poll(pending.poll_token)
    r = runner.invoke(cli.app, ["devices", "revoke", paired.device_id, "-c", str(cfg)])
    assert r.exit_code == 0
    assert svc.authenticate(paired.token) is None
    r = runner.invoke(cli.app, ["devices", "revoke", "doesnotexist", "-c", str(cfg)])
    assert r.exit_code == 1
    r = runner.invoke(cli.app, ["devices", "approve", "9999", "-c", str(cfg)])
    assert r.exit_code == 1


def test_serve_keeps_websocket_alive_with_pings(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: captured.update(kw))
    r = runner.invoke(cli.app, ["serve", "-c", str(write_config(tmp_path))])
    assert r.exit_code == 0, r.output
    assert captured["ws_ping_interval"] == 20.0 and captured["ws_ping_timeout"] == 20.0
    assert captured["port"] == 8080 and captured["proxy_headers"] is True
    assert "forwarded_allow_ips" not in captured


def test_bad_config_exits_2(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("server: {}\n", encoding="utf-8")
    r = runner.invoke(cli.app, ["pair", "-c", str(bad)])
    assert r.exit_code == 2


def test_serve_with_unknown_provider_type_exits_2(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: called.append(app))
    cfg = write_config(tmp_path)
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["providers"]["stt"]["type"] = "openai_sttt"
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    r = runner.invoke(cli.app, ["serve", "-c", str(cfg)])
    assert r.exit_code == 2, r.output
    assert "config error" in r.output
    assert "openai_sttt" in r.output
    assert called == []
