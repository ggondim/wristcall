import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from conftest import fake_config
from wristcall.app import create_app
from wristcall.config import parse_config, ProviderConfig
from wristcall.storage import open_sqlite_storage
from wristcall.users import UserService


def run(coro):
    return asyncio.run(coro)


def make_client(cfg=None):
    return TestClient(create_app(cfg or fake_config(), storage=open_sqlite_storage(":memory:")))


@pytest.fixture
def client():
    with make_client() as c:
        yield c


def h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def api_token(client, handle: str = "owner") -> str:
    users = UserService(client.app.state.storage)
    user = run(client.app.state.storage.users.by_handle(handle)) or run(users.create(handle))
    return run(users.issue_token(user.id, "test"))[1]


def device_token(client, handle: str = "owner") -> str:
    user = run(client.app.state.storage.users.by_handle(handle))
    code = run(client.app.state.pairing.create_code(user.id)).code
    return client.post("/v1/pair", json={"code": code, "device_name": "Watch"}).json()["token"]


def test_agents_crud(client):
    t = api_token(client)
    listed = client.get("/v1/agents", headers=h(t)).json()["agents"]
    assert [a["slug"] for a in listed] == ["default"]
    assert listed[0]["stt"] == {"provider": "stt"}  # detail view for the owner

    r = client.post("/v1/agents", headers=h(t), json={"slug": "coach", "display_name": "Coach", "turn_end": "manual"})
    assert r.status_code == 201
    coach = r.json()
    assert (coach["slug"], coach["turn_end"], coach["position"]) == ("coach", "manual", 1)

    assert client.get(f"/v1/agents/{coach['id']}", headers=h(t)).json()["slug"] == "coach"
    r = client.patch("/v1/agents/coach", headers=h(t), json={"vad": {"silence_ms": 1500}, "icon": "figure.run"})
    assert r.status_code == 200 and r.json()["vad"]["silence_ms"] == 1500 and r.json()["icon"] == "figure.run"

    assert client.delete("/v1/agents/coach", headers=h(t)).status_code == 204
    assert client.get("/v1/agents/coach", headers=h(t)).status_code == 404


@pytest.mark.parametrize(
    "body, status, code",
    [
        ({"slug": "Bad"}, 422, "invalid"),
        ({"slug": "default"}, 409, "conflict"),
        ({"slug": "x", "call_type": "one-shot"}, 422, "invalid"),
        ({"slug": "x", "unknown": 1}, 422, "invalid"),
    ],
)
def test_agent_errors(client, body, status, code):
    r = client.post("/v1/agents", headers=h(api_token(client)), json=body)
    assert r.status_code == status and r.json()["error"] == code and r.json()["message"]


def test_agent_limit_is_403():
    cfg = fake_config()
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"max_agents_per_user": 1})})
    with make_client(cfg) as client:
        r = client.post("/v1/agents", headers=h(api_token(client)), json={"slug": "second"})
        assert r.status_code == 403 and r.json()["error"] == "limit"


def test_secrets_never_come_back(client):
    t = api_token(client)
    stt = {"type": "openai_stt", "base_url": "https://stt.example/v1", "model": "w", "api_key": "sk-very-secret"}
    created = client.post("/v1/agents", headers=h(t), json={"slug": "own", "stt": stt})
    assert created.status_code == 201
    for r in (created, client.get("/v1/agents/own", headers=h(t)), client.get("/v1/agents", headers=h(t))):
        assert "sk-very-secret" not in r.text
    # Sending the redacted value back keeps the stored key.
    r = client.patch("/v1/agents/own", headers=h(t), json={"stt": created.json()["stt"]})
    assert r.status_code == 200
    agent = run(client.app.state.agents.get(run(client.app.state.storage.users.by_handle("owner")).id, "own"))
    assert agent.spec.stt.options()["api_key"] == "sk-very-secret"


def test_validation_errors_do_not_echo_secrets(client):
    stt = {"type": "openai_stt", "api_key": "sk-very-secret"}  # missing base_url
    r = client.post("/v1/agents", headers=h(api_token(client)), json={"slug": "own", "stt": stt})
    assert r.status_code == 422 and "sk-very-secret" not in r.text


def test_device_token_lists_summaries_but_cannot_manage(client):
    d = device_token(client)
    listed = client.get("/v1/agents", headers=h(d)).json()["agents"]
    assert set(listed[0]) == {"id", "slug", "display_name", "icon", "color", "call_type", "turn_end"}
    assert set(client.get("/v1/agents/default", headers=h(d)).json()) == set(listed[0])
    for method, path, body in [
        ("post", "/v1/agents", {"slug": "x"}),
        ("patch", "/v1/agents/default", {"icon": "x"}),
        ("delete", "/v1/agents/default", None),
        ("get", "/v1/providers", None),
        ("get", "/v1/devices", None),
        ("post", "/v1/pairing-codes", None),
    ]:
        r = client.request(method.upper(), path, headers=h(d), json=body)
        assert r.status_code == 403 and r.json()["error"] == "forbidden", path


def test_no_token_is_401(client):
    for method, path in [("GET", "/v1/agents"), ("POST", "/v1/agents"), ("GET", "/v1/devices"), ("POST", "/v1/pairing-codes")]:
        r = client.request(method, path, json={} if method == "POST" else None)
        assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert client.get("/v1/agents", headers=h("wc_pat_nope")).status_code == 401


def test_users_are_isolated(client):
    alice, bob = api_token(client), api_token(client, "bob")
    assert client.get("/v1/agents", headers=h(bob)).json() == {"agents": []}
    assert client.get("/v1/agents/default", headers=h(bob)).status_code == 404
    assert client.patch("/v1/agents/default", headers=h(bob), json={"icon": "x"}).status_code == 404
    assert client.delete("/v1/agents/default", headers=h(bob)).status_code == 404
    assert client.post("/v1/agents", headers=h(bob), json={"slug": "default"}).status_code == 201  # same slug, other user
    alice_device = device_token(client)
    devices = client.get("/v1/devices", headers=h(alice)).json()["devices"]
    assert [d["name"] for d in devices] == ["Watch"]
    assert client.get("/v1/devices", headers=h(bob)).json() == {"devices": []}
    assert client.delete(f"/v1/devices/{devices[0]['id']}", headers=h(bob)).status_code == 404
    assert client.get("/v1/me", headers=h(alice_device)).status_code == 200


def test_providers_name_webhooks():
    cfg = fake_config()
    cfg.providers["inbox"] = ProviderConfig(type="webhook", url="https://n8n.example/webhook/abc")
    with make_client(cfg) as c:
        body = c.get("/v1/providers", headers=h(api_token(c))).json()
    assert {"name": "inbox", "kind": "webhook"} in body["providers"]


def test_providers(client):
    body = client.get("/v1/providers", headers=h(api_token(client))).json()
    assert body == {
        "providers": [{"name": "stt", "kind": "stt"}, {"name": "llm", "kind": "action"}, {"name": "tts", "kind": "tts"}],
        "custom_endpoints": True,
        "public_only": False,
        "history_only_one_way": False,
    }


def history_only_client():
    cfg = fake_config()
    cfg.limits.history_only_one_way = True
    return make_client(cfg)


def test_providers_lists_history_only_one_way(client):
    assert client.get("/v1/providers", headers=h(api_token(client))).json()["history_only_one_way"] is False
    with history_only_client() as c:
        assert c.get("/v1/providers", headers=h(api_token(c))).json()["history_only_one_way"] is True


HOOK = {"type": "webhook", "url": "https://hooks.example/in"}


def test_action_null_is_ignored_without_the_limit(client):
    token = api_token(client)
    assert client.post("/v1/agents", headers=h(token), json={"slug": "note", "call_type": "one-shot", "action": HOOK}).status_code == 201
    r = client.patch("/v1/agents/note", headers=h(token), json={"action": None})
    assert r.status_code == 200 and r.json()["action"] == HOOK


def test_history_only_agents_through_the_api():
    with history_only_client() as c:
        token = api_token(c)
        r = c.post("/v1/agents", headers=h(token), json={"slug": "note", "call_type": "one-shot"})
        assert r.status_code == 201 and r.json()["action"] is None
        assert c.patch("/v1/agents/note", headers=h(token), json={"action": HOOK}).json()["action"] == HOOK
        assert c.patch("/v1/agents/note", headers=h(token), json={"action": None}).json()["action"] is None
        r = c.patch("/v1/agents/default", headers=h(token), json={"action": None})
        assert r.status_code == 422 and r.json()["message"] == "action: required for conversation agents"


def public_only_client():
    cfg = fake_config()
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"public_only": True})})
    return make_client(cfg)


def test_create_agent_with_internal_url_is_422():
    stt = {"type": "openai_stt", "base_url": "https://speaches:8000/v1", "model": "w", "api_key": "sk-secret"}
    with public_only_client() as c:
        t = api_token(c)
        r = c.post("/v1/agents", headers=h(t), json={"slug": "own", "stt": stt})
        assert r.status_code == 422
        assert r.json() == {"error": "url_not_public", "message": "stt: the host is a local or internal name"}
        assert "speaches" not in r.text and "sk-secret" not in r.text
        assert c.get("/v1/agents/own", headers=h(t)).status_code == 404
        # PATCH too: the stored agent stays as it was.
        r = c.patch("/v1/agents/default", headers=h(t), json={"stt": {**stt, "base_url": "http://x.example.com/v1"}})
        assert r.status_code == 422 and r.json()["error"] == "url_not_public"


def test_providers_lists_public_only(client):
    assert client.get("/v1/providers", headers=h(api_token(client))).json()["public_only"] is False
    with public_only_client() as c:
        assert c.get("/v1/providers", headers=h(api_token(c))).json()["public_only"] is True


def test_pairing_code_pairs_a_watch_to_the_token_owner(client):
    t = api_token(client, "bob")
    r = client.post("/v1/pairing-codes", headers=h(t))
    assert r.status_code == 201
    body = r.json()
    assert len(body["code"]) == 8 and body["via_directory"] is False and body["server_url"] == "http://testserver"
    token = client.post("/v1/pair", json={"code": body["code"], "device_name": "Bob's Watch"}).json()["token"]
    assert client.get("/v1/me", headers=h(token)).json()["user"]["handle"] == "bob"
    devices = client.get("/v1/devices", headers=h(t)).json()["devices"]
    assert client.delete(f"/v1/devices/{devices[0]['id']}", headers=h(t)).status_code == 204
    assert client.get("/v1/me", headers=h(token)).status_code == 401


def test_pairing_code_respects_the_device_limit():
    cfg = fake_config()
    cfg = cfg.model_copy(update={"limits": cfg.limits.model_copy(update={"max_devices_per_user": 1})})
    with make_client(cfg) as client:
        device_token(client)
        r = client.post("/v1/pairing-codes", headers=h(api_token(client)))
        assert r.status_code == 403 and r.json()["error"] == "limit"


@respx.mock
def test_pairing_code_with_directory():
    route = respx.post("https://dir.test/v1/codes").mock(return_value=httpx.Response(201, json={}))
    data = fake_config().model_dump(mode="json")
    data["server"] = {"public_url": "https://wc.test", "directory_url": "https://dir.test"}
    with make_client(parse_config(data, {})) as client:
        r = client.post("/v1/pairing-codes", headers=h(api_token(client)))
        assert r.status_code == 201 and r.json()["via_directory"] is True
        assert route.called


def test_auth_comes_before_body_parsing(client):
    for method, path in [("post", "/v1/agents"), ("patch", "/v1/agents/default")]:
        for kwargs in [{}, {"content": b"{not json"}, {"json": [1]}]:
            r = client.request(method.upper(), path, **kwargs)
            assert r.status_code == 401 and r.json()["error"] == "unauthorized" and "detail" not in r.json()
        d = device_token(client)
        r = client.request(method.upper(), path, headers=h(d), json=[{"api_key": "sk-secret"}])
        assert r.status_code == 403 and r.json()["error"] == "forbidden" and "sk-secret" not in r.text


def test_bad_bodies_are_invalid_without_echo(client):
    t = api_token(client)
    for method, path in [("post", "/v1/agents"), ("patch", "/v1/agents/default")]:
        for kwargs in [
            {"json": [{"api_key": "sk-secret"}]},
            {"json": "sk-secret"},
            {"content": b'{"api_key": "sk-secret"', "headers": {"Content-Type": "application/json"}},
            {},
        ]:
            headers = {**h(t), **kwargs.pop("headers", {})}
            r = client.request(method.upper(), path, headers=headers, **kwargs)
            assert r.status_code == 422, (method, kwargs)
            assert r.json()["error"] == "invalid" and "detail" not in r.json()
            assert "sk-secret" not in r.text


def test_device_cannot_read_another_users_agent_by_id(client):
    bob = api_token(client, "bob")
    bob_agent = client.post("/v1/agents", headers=h(bob), json={"slug": "private"}).json()["id"]
    alice_device = device_token(client)
    assert client.get(f"/v1/agents/{bob_agent}", headers=h(alice_device)).status_code == 404


@respx.mock
def test_pairing_code_directory_conflicts_are_502():
    route = respx.post("https://dir.test/v1/codes").mock(return_value=httpx.Response(409, json={}))
    data = fake_config().model_dump(mode="json")
    data["server"] = {"public_url": "https://wc.test", "directory_url": "https://dir.test"}
    with make_client(parse_config(data, {})) as client:
        r = client.post("/v1/pairing-codes", headers=h(api_token(client)))
        assert r.status_code == 502 and r.json()["error"] == "directory"
        assert route.call_count == 3


def test_deeply_nested_body_is_invalid(client):
    t = api_token(client)
    for method, path in [("post", "/v1/agents"), ("patch", "/v1/agents/default")]:
        for content in [b"[" * 100000, b'{"a":' * 100000]:
            headers = {**h(t), "Content-Type": "application/json"}
            r = client.request(method.upper(), path, headers=headers, content=content)
            assert r.status_code == 422, (method, content[:8])
            assert r.json()["error"] == "invalid" and "detail" not in r.json()


def test_create_agent_with_color(client):
    t = api_token(client)
    r = client.post("/v1/agents", headers=h(t), json={"slug": "inky", "color": "indigo"})
    assert r.status_code == 201 and r.json()["color"] == "indigo"
    assert client.get("/v1/agents/inky", headers=h(t)).json()["color"] == "indigo"
    listed = {a["slug"]: a for a in client.get("/v1/agents", headers=h(t)).json()["agents"]}
    assert listed["inky"]["color"] == "indigo"
    assert listed["default"]["color"] is None


@pytest.mark.parametrize("color", ["magenta", "Indigo", "INDIGO", "", 3])
def test_agent_color_is_validated(client, color):
    t = api_token(client)
    r = client.post("/v1/agents", headers=h(t), json={"slug": "bad", "color": color})
    assert r.status_code == 422
    assert client.post("/v1/agents", headers=h(t), json={"slug": "ok"}).status_code == 201
    assert client.patch("/v1/agents/ok", headers=h(t), json={"color": color}).status_code == 422


def test_update_keeps_color_when_absent(client):
    t = api_token(client)
    client.post("/v1/agents", headers=h(t), json={"slug": "keep", "color": "mint"})
    r = client.patch("/v1/agents/keep", headers=h(t), json={"display_name": "Kept"})
    assert r.status_code == 200 and r.json()["color"] == "mint"
    r = client.patch("/v1/agents/keep", headers=h(t), json={"color": "teal"})
    assert r.status_code == 200 and r.json()["color"] == "teal"
    assert client.get("/v1/agents/keep", headers=h(t)).json()["color"] == "teal"
