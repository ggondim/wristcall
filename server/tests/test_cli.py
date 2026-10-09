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


def test_pair_needs_a_user_choice_with_several_users(tmp_path):
    cfg = write_config(tmp_path)
    ok(cfg, "users", "add", "bob")
    r = invoke(cfg, "pair")
    assert r.exit_code == 1 and "--user" in r.output
    out = ok(cfg, "pair", "--user", "bob")
    device = run(svc_for(tmp_path).pair(code_in(out), "Watch"))
    assert run(svc_for(tmp_path).authenticate(device.token)).user_id != run(open_sqlite_storage(tmp_path / "data").users.by_handle("owner")).id


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


def test_users_and_tokens(tmp_path):
    cfg = write_config(tmp_path)
    assert "owner" in ok(cfg, "users", "list")
    ok(cfg, "users", "edit", "owner", "--handle", "gustavo", "--name", "Gustavo")
    ok(cfg, "users", "add", "bob", "--name", "Bob")
    out = ok(cfg, "users", "list")
    assert "gustavo  Gustavo  1 agent(s)" in out and "bob  Bob  0 agent(s)" in out
    assert invoke(cfg, "users", "add", "bob").exit_code == 1
    assert invoke(cfg, "users", "add", "Bad Handle").exit_code == 1

    out = ok(cfg, "users", "tokens", "add", "--user", "bob", "--name", "laptop")
    token = out.strip().splitlines()[-1]
    assert token.startswith("wc_pat_")
    listed = ok(cfg, "users", "tokens", "list", "--user", "bob")
    assert "laptop" in listed and token not in listed and "never" in listed
    token_id = listed.split()[0]
    ok(cfg, "users", "tokens", "revoke", token_id)
    assert "No API tokens" in ok(cfg, "users", "tokens", "list", "--user", "bob")
    assert invoke(cfg, "users", "tokens", "revoke", token_id).exit_code == 1

    assert invoke(cfg, "users", "rm", "bob", input="n\n").exit_code == 1
    ok(cfg, "users", "rm", "bob", "--yes")
    assert "bob" not in ok(cfg, "users", "list")


def test_agents_add_list_show_edit_rm(tmp_path):
    cfg = write_config(tmp_path)
    out = ok(cfg, "agents", "list")
    assert "default  Test  waveform  conversation  turn_end=auto  stt=stt action=llm tts=tts" in out

    ok(cfg, "agents", "add", "coach", "--name", "Coach", "--icon", "figure.run", "--turn-end", "manual",
       "--silence-ms", "1500", "--language", "pt", "--prompt", "Be a coach.")
    lines = ok(cfg, "agents", "list").splitlines()
    assert lines[1].startswith("1  coach  Coach  figure.run  conversation  turn_end=manual")

    shown = json.loads(ok(cfg, "agents", "show", "coach"))
    assert shown["vad"]["silence_ms"] == 1500 and shown["language"] == "pt" and shown["system_prompt"] == "Be a coach."

    ok(cfg, "agents", "edit", "coach", "--slug", "trainer", "--position", "0")
    assert [line.split()[1] for line in ok(cfg, "agents", "list").splitlines()] == ["trainer", "default"]
    ok(cfg, "agents", "edit", "trainer", "--position", "5")  # past the end: last
    assert [line.split()[:2] for line in ok(cfg, "agents", "list").splitlines()] == [["0", "default"], ["1", "trainer"]]

    assert invoke(cfg, "agents", "rm", "trainer", input="n\n").exit_code == 1
    ok(cfg, "agents", "rm", "trainer", "--yes")
    assert "trainer" not in ok(cfg, "agents", "list")


def test_agents_show_round_trips_through_from_json(tmp_path):
    cfg = write_config(tmp_path)
    stt = '{"type": "openai_stt", "base_url": "https://stt.example/v1", "model": "w", "api_key": "sk-secret"}'
    ok(cfg, "agents", "add", "own", "--stt", stt)
    shown = ok(cfg, "agents", "show", "own")
    assert "sk-secret" not in shown and '"api_key": "***"' in shown
    edited = json.loads(shown)
    edited["display_name"] = "Mine"
    path = tmp_path / "own.json"
    path.write_text(json.dumps(edited), encoding="utf-8")
    ok(cfg, "agents", "edit", "own", "--from-json", str(path))
    st = open_sqlite_storage(tmp_path / "data")
    owner = run(st.users.by_handle("owner"))
    record = run(st.agents.get(owner.id, "own"))
    assert record.display_name == "Mine" and record.spec["stt"]["api_key"] == "sk-secret"


def test_agents_from_json_on_stdin(tmp_path):
    cfg = write_config(tmp_path)
    ok(cfg, "agents", "add", "piped", "--from-json", "-", "--name", "Flag wins", input='{"display_name": "From JSON", "icon": "star"}')
    shown = json.loads(ok(cfg, "agents", "show", "piped"))
    assert (shown["display_name"], shown["icon"]) == ("Flag wins", "star")


def test_agents_errors_exit_1_with_a_message(tmp_path):
    cfg = write_config(tmp_path)
    for args, fragment in [
        (("agents", "add", "default"), "already exists"),
        (("agents", "add", "x", "--call-type", "one-shot"), "not supported yet"),
        (("agents", "add", "x", "--stt", "nope"), "nope"),
        (("agents", "add", "x", "--stt", "{not json"), "invalid JSON"),
        (("agents", "add", "x", "--turn-end", "sometimes"), "turn_end"),
        (("agents", "show", "missing"), "not found"),
        (("agents", "edit", "missing", "--name", "x"), "not found"),
        (("agents", "list", "--user", "nobody"), "user not found"),
    ]:
        r = invoke(cfg, *args)
        assert r.exit_code == 1 and fragment in r.output, (args, r.output)


def test_agents_add_with_several_providers_asks_to_choose(tmp_path):
    providers = {"stt": {"type": "fake_stt"}, "stt2": {"type": "fake_stt"}, "llm": {"type": "echo_chat"}, "tts": {"type": "tone_tts"}}
    cfg = write_config(tmp_path, profiles=False, providers=providers)
    ok(cfg, "users", "add", "alice")
    r = invoke(cfg, "agents", "add", "x")
    assert r.exit_code == 1 and "this server offers: stt, stt2" in r.output
    ok(cfg, "agents", "add", "x", "--stt", "stt2")


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


def test_agents_add_accepts_show_output(tmp_path):
    cfg = write_config(tmp_path)
    path = tmp_path / "default.json"
    path.write_text(ok(cfg, "agents", "show", "default"), encoding="utf-8")
    ok(cfg, "agents", "add", "copy", "--from-json", str(path))
    original = json.loads(ok(cfg, "agents", "show", "default"))
    copy = json.loads(ok(cfg, "agents", "show", "copy"))
    assert copy["slug"] == "copy" and copy["id"] != original["id"]
    assert (copy["display_name"], copy["turn_end"]) == (original["display_name"], original["turn_end"])


def test_agents_unreadable_files_exit_1_without_traceback(tmp_path):
    cfg = write_config(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe")
    for args, fragment in [
        (("agents", "add", "x", "--from-json", str(tmp_path / "missing.json")), "cannot read"),
        (("agents", "add", "x", "--prompt-file", str(tmp_path / "missing.txt")), "cannot read"),
        (("agents", "edit", "default", "--from-json", str(tmp_path)), "cannot read"),
        (("agents", "add", "x", "--from-json", str(bad)), "not UTF-8"),
        (("agents", "add", "x", "--prompt-file", str(bad)), "not UTF-8"),
    ]:
        r = invoke(cfg, *args)
        assert r.exit_code == 1 and fragment in r.output and "Traceback" not in r.output, (args, r.output)
