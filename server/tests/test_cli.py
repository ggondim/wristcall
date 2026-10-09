import asyncio
import json
import re

import httpx
import respx
import yaml
from typer.testing import CliRunner

from wristcall import cli
from wristcall.pairing import Paired, PairingService
from wristcall.storage import open_sqlite_storage

runner = CliRunner()


def write_config(tmp_path, profiles=True, providers=None, **server):
    data = {
        "server": {"public_url": "https://wc.example.test", "data_dir": str(tmp_path / "data"), **server},
        "providers": providers or {
            "stt": {"type": "fake_stt"},
            "llm": {"type": "echo_chat"},
            "tts": {"type": "tone_tts"},
        },
    }
    if profiles:
        data["profiles"] = {"default": {"display_name": "Test", "stt": "stt", "responder": "llm", "tts": "tts", "vad": {"type": "energy"}}}
    path = tmp_path / "wristcall.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def invoke(cfg, *args, input=None):
    return runner.invoke(cli.app, [*args, "-c", str(cfg)], input=input)


def ok(cfg, *args, input=None) -> str:
    r = invoke(cfg, *args, input=input)
    assert r.exit_code == 0, r.output
    return r.output


def code_in(output: str) -> str:
    return re.search(r"Pairing code: (\d{4}) (\d{4})", output).group(0)[-9:].replace(" ", "")


def svc_for(tmp_path, approval="code"):
    return PairingService(open_sqlite_storage(tmp_path / "data"), approval)


def run(coro):
    return asyncio.run(coro)


def test_pair_without_directory_prints_url_and_valid_code(tmp_path):
    out = ok(write_config(tmp_path), "pair")
    assert "https://wc.example.test" in out and "owner" in out
    paired = run(svc_for(tmp_path).pair(code_in(out), "Watch"))
    assert isinstance(paired, Paired)


def test_pair_output_matches_the_watch_integration_tests(tmp_path):
    # TestServer.swift: the line starts with "Pairing code:" and has exactly 8 digits.
    out = ok(write_config(tmp_path), "pair")
    (line,) = [x for x in out.splitlines() if x.startswith("Pairing code:")]
    assert len([c for c in line if c.isdigit()]) == 8


@respx.mock
def test_pair_registers_in_directory(tmp_path):
    route = respx.post("https://dir.test/v1/codes").mock(return_value=httpx.Response(201, json={"code": "x", "expires_at": 0}))
    out = ok(write_config(tmp_path, directory_url="https://dir.test"), "pair")
    assert json.loads(route.calls.last.request.content) == {"url": "https://wc.example.test", "code": code_in(out)}
    assert "Enter server URL" not in out


@respx.mock
def test_pair_retries_on_directory_conflict(tmp_path):
    route = respx.post("https://dir.test/v1/codes").mock(
        side_effect=[httpx.Response(409, json={"error": "conflict"}), httpx.Response(201, json={})]
    )
    out = ok(write_config(tmp_path, directory_url="https://dir.test"), "pair")
    first = json.loads(route.calls[0].request.content)["code"]
    assert json.loads(route.calls[1].request.content)["code"] == code_in(out)
    assert first != code_in(out)


@respx.mock
def test_pair_with_directory_down_falls_back_to_url(tmp_path):
    respx.post("https://dir.test/v1/codes").mock(side_effect=httpx.ConnectError("down"))
    r = invoke(write_config(tmp_path, directory_url="https://dir.test"), "pair")
    assert r.exit_code == 0
    assert "https://wc.example.test" in r.output


def test_pair_without_any_user_explains(tmp_path):
    r = invoke(write_config(tmp_path, profiles=False), "pair")
    assert r.exit_code == 1 and "wristcall users add" in r.output


def test_devices_list_approve_revoke(tmp_path):
    cfg = write_config(tmp_path, pairing_approval="manual")
    ok(cfg, "devices", "list")  # bootstrap: creates owner and imports the profile
    svc = svc_for(tmp_path, "manual")
    pending = run(svc.pair(None, "My Apple Watch"))
    out = ok(cfg, "devices", "list")
    assert pending.request_id in out and "My Apple Watch" in out
    assert "Approved" in ok(cfg, "devices", "approve", pending.request_id)
    paired = run(svc.poll(pending.poll_token))
    assert "owner" in ok(cfg, "devices", "list")
    ok(cfg, "devices", "revoke", paired.device_id)
    assert run(svc.authenticate(paired.token)) is None
    assert invoke(cfg, "devices", "revoke", "doesnotexist").exit_code == 1
    assert invoke(cfg, "devices", "approve", "9999").exit_code == 1


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
    assert invoke(bad, "pair").exit_code == 2


def test_serve_with_unknown_provider_type_exits_2(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: called.append(app))
    cfg = write_config(tmp_path)
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["providers"]["stt"]["type"] = "openai_sttt"
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    r = runner.invoke(cli.app, ["serve", "-c", str(cfg)])
    assert r.exit_code == 2, r.output
    assert "config error" in r.output and "openai_sttt" in r.output
    assert called == []
