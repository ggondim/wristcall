import pytest
from pymongo import MongoClient

from conftest import ISSUER, mongo_url, serve
from wristcall_cloud.app import create_app
from wristcall_cloud.config import CloudConfig
from wristcall_cloud.store import Store

H_A = {"Authorization": "Bearer token-a"}
H_B = {"Authorization": "Bearer token-b"}
H_WATCH = {"Authorization": "Bearer token-watch"}
KEY_A = f"{ISSUER}#a"


@pytest.fixture(autouse=True)
def tokens(fake_verifier):
    fake_verifier.add("token-a", "a", client_id="client-ios")
    fake_verifier.add("token-b", "b", client_id="client-pwa")
    fake_verifier.add("token-watch", "a", client_id="client-watch")


def limited_client(mongo_db, fake_verifier, **limits):
    cfg = CloudConfig(mongo_url=mongo_url(), issuer=ISSUER, clients={"ios": "client-ios"}, **limits)
    yield from serve(create_app(cfg, store=Store(mongo_db), verifier=fake_verifier), mongo_db)


def agent(i: int, **over):
    return {"id": f"ag{i}", "slug": f"agent-{i}", "display_name": f"Agent {i}", "icon": "waveform", "call_type": "conversation", **over}


def add_server(client, name="home", url="https://home.test", headers=H_A, **extra):
    r = client.post("/v1/servers", json={"name": name, "url": url, **extra}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def test_account_is_created_on_first_access(client, mongo_db):
    assert client.get("/v1/account").status_code == 401
    r = client.get("/v1/account", headers=H_A)
    assert r.status_code == 200
    body = r.json()
    assert body["account"] == KEY_A
    assert body["servers"] == 0
    assert isinstance(body["created_at"], float)
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        doc = sync[mongo_db.name]["accounts"].find_one({"_id": KEY_A})
    assert doc["created_at"] == body["created_at"]
    first_seen = doc["last_seen_at"]
    again = client.get("/v1/account", headers=H_A).json()
    assert again["created_at"] == body["created_at"]
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        assert sync[mongo_db.name]["accounts"].find_one({"_id": KEY_A})["last_seen_at"] >= first_seen


def test_writes_create_the_account(client, mongo_db):
    add_server(client)
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        assert sync[mongo_db.name]["accounts"].count_documents({"_id": KEY_A}) == 1
    assert client.get("/v1/account", headers=H_A).json()["servers"] == 1


def test_server_crud(client):
    created = add_server(client)
    assert created["id"].startswith("srv_") and len(created["id"]) == 4 + 12
    assert created["kind"] == "self-hosted" and created["linked"] is False and created["agents"] == []
    assert client.get("/v1/servers", headers=H_A).json() == {"servers": [created]}

    r = client.patch(f"/v1/servers/{created['id']}", json={"name": "casa", "linked": True}, headers=H_A)
    assert r.status_code == 200
    patched = r.json()
    assert patched["name"] == "casa" and patched["linked"] is True and patched["url"] == "https://home.test"
    assert patched["updated_at"] >= created["updated_at"] and patched["created_at"] == created["created_at"]
    assert client.get(f"/v1/servers/{created['id']}", headers=H_A).json() == patched

    for body in ({"url": "https://other.test"}, {"kind": "cloud"}, {"name": "x", "url": "https://o.test"}, {}, {"name": ""}):
        r = client.patch(f"/v1/servers/{created['id']}", json=body, headers=H_A)
        assert r.status_code == 422, body
        assert r.json()["error"] == "invalid"

    assert client.delete(f"/v1/servers/{created['id']}", headers=H_A).status_code == 204
    assert client.get(f"/v1/servers/{created['id']}", headers=H_A).status_code == 404
    assert client.delete(f"/v1/servers/{created['id']}", headers=H_A).status_code == 404
    assert client.get("/v1/servers", headers=H_A).json() == {"servers": []}


def test_servers_are_listed_by_creation(client):
    ids = [add_server(client, name=f"s{i}", url=f"https://s{i}.test")["id"] for i in range(4)]
    assert [s["id"] for s in client.get("/v1/servers", headers=H_A).json()["servers"]] == ids


def test_watch_token_cannot_delete_account(client):
    add_server(client)
    r = client.delete("/v1/account", headers=H_WATCH)
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"
    assert client.get("/v1/account", headers=H_WATCH).json()["servers"] == 1
    assert client.delete("/v1/account", headers=H_B).status_code == 204  # the pwa client may


def test_url_is_normalized_and_unique_per_account(client):
    first = add_server(client, url="https://x.test/")
    assert first["url"] == "https://x.test"
    r = client.post("/v1/servers", json={"name": "dup", "url": "https://x.test"}, headers=H_A)
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"
    assert client.post("/v1/servers", json={"name": "dup", "url": "https://X.test//"}, headers=H_A).status_code == 409
    assert add_server(client, url="http://x.test:8080/base/")["url"] == "http://x.test:8080/base"
    assert add_server(client, url="https://x.test", headers=H_B)["url"] == "https://x.test"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "url": "https://x.test"},
        {"name": "   ", "url": "https://x.test"},
        {"name": "a" * 65, "url": "https://x.test"},
        {"name": "a\nb", "url": "https://x.test"},
        {"name": 1, "url": "https://x.test"},
        {"url": "https://x.test"},
        {"name": "a"},
        {"name": "a", "url": "ftp://x.test"},
        {"name": "a", "url": "x.test"},
        {"name": "a", "url": "https:///path"},
        {"name": "a", "url": "https://u:p@x.test"},
        {"name": "a", "url": "https://u@x.test"},
        {"name": "a", "url": "https://x.test/?q=1"},
        {"name": "a", "url": "https://x.test/#f"},
        {"name": "a", "url": "https://x.test:abc"},
        {"name": "a", "url": "https://x.test/" + "p" * 2048},
        {"name": "a", "url": "https://x .test"},
        {"name": "a", "url": None},
        {"name": "a", "url": "https://x.test", "kind": "other"},
        {"name": "a\n", "url": "https://x.test", "extra": 1},
        {"name": "a", "url": "https://x.test", "linked": "yes"},
        {"name": "a", "url": "https://x.test", "linked": 1},
    ],
)
def test_invalid_servers_are_422(client, body):
    r = client.post("/v1/servers", json=body, headers=H_A)
    assert r.status_code == 422, body
    assert r.json()["error"] == "invalid"
    assert "p:p" not in r.text and "u:p" not in r.text


def test_body_must_be_an_object_and_auth_comes_first(client):
    r = client.post("/v1/servers", content=b"[1]", headers={**H_A, "Content-Type": "application/json"})
    assert r.status_code == 422
    assert client.post("/v1/servers", json={"name": "a"}).status_code == 401


def test_server_accepts_cloud_kind_linked_and_name_is_stripped(client):
    s = add_server(client, name="  nuvem  ", url="http://192.168.0.5:8765", kind="cloud", linked=True)
    assert (s["name"], s["kind"], s["linked"]) == ("nuvem", "cloud", True)


def test_server_limit(mongo_db, fake_verifier):
    for c in limited_client(mongo_db, fake_verifier, max_servers=2):
        add_server(c, url="https://1.test")
        add_server(c, url="https://2.test")
        r = c.post("/v1/servers", json={"name": "3", "url": "https://3.test"}, headers=H_A)
        assert r.status_code == 403
        assert r.json()["error"] == "limit"
        assert add_server(c, url="https://1.test", headers=H_B)  # limit is per account
        # freeing a slot allows a new one
        sid = c.get("/v1/servers", headers=H_A).json()["servers"][0]["id"]
        assert c.delete(f"/v1/servers/{sid}", headers=H_A).status_code == 204
        add_server(c, url="https://3.test")


def test_agents_snapshot_and_global_list(client):
    s1 = add_server(client, name="one", url="https://one.test")
    s2 = add_server(client, name="two", url="https://two.test")
    other = add_server(client, name="theirs", url="https://theirs.test", headers=H_B)
    r = client.put(f"/v1/servers/{s2['id']}/agents", json={"agents": [agent(3), agent(4, call_type="one-shot")]}, headers=H_A)
    assert r.status_code == 200
    assert r.json() == {"agents": [agent(3), agent(4, call_type="one-shot")]}
    client.put(f"/v1/servers/{s1['id']}/agents", json={"agents": [agent(1), agent(2, call_type="monologue")]}, headers=H_A)
    client.put(f"/v1/servers/{other['id']}/agents", json={"agents": [agent(9)]}, headers=H_B)

    got = client.get("/v1/agents", headers=H_A).json()["agents"]
    assert [(a["id"], a["server_id"], a["server_name"], a["server_url"]) for a in got] == [
        ("ag1", s1["id"], "one", "https://one.test"),
        ("ag2", s1["id"], "one", "https://one.test"),
        ("ag3", s2["id"], "two", "https://two.test"),
        ("ag4", s2["id"], "two", "https://two.test"),
    ]
    assert got[0]["slug"] == "agent-1" and got[1]["call_type"] == "monologue"
    assert client.get(f"/v1/servers/{s1['id']}", headers=H_A).json()["agents"] == [agent(1), agent(2, call_type="monologue")]

    # PUT replaces the whole list
    client.put(f"/v1/servers/{s1['id']}/agents", json={"agents": []}, headers=H_A)
    assert [a["id"] for a in client.get("/v1/agents", headers=H_A).json()["agents"]] == ["ag3", "ag4"]


def test_agents_limit_and_validation(client):
    sid = add_server(client)["id"]
    put = lambda body: client.put(f"/v1/servers/{sid}/agents", json=body, headers=H_A)  # noqa: E731
    assert put({"agents": [agent(i) for i in range(50)]}).status_code == 200
    r = put({"agents": [agent(i) for i in range(51)]})
    assert r.status_code == 403
    assert r.json()["error"] == "limit"
    assert len(client.get(f"/v1/servers/{sid}", headers=H_A).json()["agents"]) == 50  # untouched by the refusal

    bad = [
        {"agents": [agent(1, call_type="chat")]},
        {"agents": [agent(1), agent(1, slug="other")]},
        {"agents": [agent(1, extra="x")]},
        {"agents": [agent(1, icon="Wave Form")]},
        {"agents": [agent(1, icon="")]},
        {"agents": [agent(1, id="a b")]},
        {"agents": [agent(1, slug="a" * 65)]},
        {"agents": [agent(1, display_name="")]},
        {"agents": [agent(1, display_name=5)]},
        {"agents": [{"id": "x"}]},
        {"agents": ["x"]},
        {"agents": "x"},
        {"agents": [agent(1)], "more": 1},
        {},
    ]
    for body in bad:
        r = put(body)
        assert r.status_code == 422, body
        assert r.json()["error"] == "invalid"
    assert len(client.get(f"/v1/servers/{sid}", headers=H_A).json()["agents"]) == 50

    assert put({"agents": [agent(1, icon="person.wave.2.fill", id="A_b-1")]}).status_code == 200


def test_agents_limit_follows_config(mongo_db, fake_verifier):
    for c in limited_client(mongo_db, fake_verifier, max_agents_per_server=2):
        sid = add_server(c)["id"]
        assert c.put(f"/v1/servers/{sid}/agents", json={"agents": [agent(1), agent(2)]}, headers=H_A).status_code == 200
        assert c.put(f"/v1/servers/{sid}/agents", json={"agents": [agent(1), agent(2), agent(3)]}, headers=H_A).status_code == 403


def test_other_accounts_servers_are_invisible(client):
    a = client.post("/v1/servers", json={"name": "home", "url": "https://home.test"}, headers=H_A).json()
    for method, path in [
        ("GET", f"/v1/servers/{a['id']}"),
        ("PATCH", f"/v1/servers/{a['id']}"),
        ("DELETE", f"/v1/servers/{a['id']}"),
        ("PUT", f"/v1/servers/{a['id']}/agents"),
    ]:
        body = {"agents": []} if method == "PUT" else ({"name": "x"} if method == "PATCH" else None)
        r = client.request(method, path, json=body, headers=H_B)
        assert r.status_code == 404, (method, path)
        assert r.json()["error"] == "not_found"
    assert client.get("/v1/servers", headers=H_B).json() == {"servers": []}
    assert client.get("/v1/agents", headers=H_B).json() == {"agents": []}
    untouched = client.get(f"/v1/servers/{a['id']}", headers=H_A).json()
    assert untouched["name"] == "home" and untouched["agents"] == []


def test_delete_account_removes_servers(client, mongo_db):
    add_server(client, url="https://1.test")
    add_server(client, url="https://2.test")
    keep = add_server(client, url="https://1.test", headers=H_B)
    assert client.delete("/v1/account", headers=H_A).status_code == 204
    with MongoClient(mongo_url(), serverSelectionTimeoutMS=5000) as sync:
        db = sync[mongo_db.name]
        assert db["servers"].count_documents({"account": KEY_A}) == 0
        assert db["accounts"].count_documents({"_id": KEY_A}) == 0
        assert db["servers"].count_documents({"account": f"{ISSUER}#b"}) == 1
    assert client.get(f"/v1/servers/{keep['id']}", headers=H_B).status_code == 200
    assert client.get("/v1/servers", headers=H_A).json() == {"servers": []}


def test_responses_never_include_account_field_in_servers(client):
    s = add_server(client)
    client.put(f"/v1/servers/{s['id']}/agents", json={"agents": [agent(1)]}, headers=H_A)
    keys = {"id", "name", "url", "kind", "linked", "agents", "created_at", "updated_at"}
    assert set(s) == keys
    assert set(client.get(f"/v1/servers/{s['id']}", headers=H_A).json()) == keys
    assert set(client.patch(f"/v1/servers/{s['id']}", json={"linked": True}, headers=H_A).json()) == keys
    assert set(client.get("/v1/servers", headers=H_A).json()["servers"][0]) == keys
    assert "account" not in client.get("/v1/agents", headers=H_A).text
    assert '"_id"' not in client.get("/v1/agents", headers=H_A).text
