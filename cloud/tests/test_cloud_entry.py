"""The managed Cloud entry in the agenda: shown (with its agents read from `cloud_agents`) only to apps that send a
recent enough `X-Wristcall-Client`, managed by the Cloud, outside the server limit. Older `kind: cloud` entries
the apps made stay as they were."""

import functools
import time
from dataclasses import replace

import pytest
from node_fixtures import PUBLIC_URL, db, grant_plan, node_config, served_node, signing_key  # noqa: F401
from test_sessions_api import bearer, create_account

from wristcall_cloud.config import ProvisionSettings
from wristcall_cloud.provision import provision

SETTINGS = ProvisionSettings(PUBLIC_URL, "en", "stt-default")
MINIMUMS = {"ios": (0, 3, 0), "web": (0, 4, 0)}
NEW = {"X-Wristcall-Client": "ios/0.3.0"}
VOICE_NOTES = {
    "slug": "voice-notes", "display_name": "Voice notes", "icon": "note.text", "call_type": "one-shot",
    "color": "yellow",
}  # fmt: skip
SELF_HOSTED = {"name": "home", "url": "https://home.test"}


def provisioned(c, mongo_db, plan: str = "full") -> tuple[str, dict[str, str]]:
    """A new account on `plan`, provisioned: (account id, headers with its access token)."""
    created = create_account(c)
    account = created["account"]
    grant_plan(mongo_db, account, plan)
    c.portal.call(functools.partial(provision, c.app.state.store, c.app.state.cloud, account, plan, SETTINGS))
    return account, bearer(created["access_token"])


@pytest.fixture
def agenda(node_config, mongo_db, fake_verifier, signing_key):  # noqa: F811
    """(client, account, headers, cloud entry id, self-hosted server id)."""
    config = replace(node_config, cloud_entry_clients=MINIMUMS)
    for c in served_node(config, mongo_db, fake_verifier, signing_key):
        account, headers = provisioned(c, mongo_db)
        r = c.post("/v1/servers", json=SELF_HOSTED, headers=headers)
        assert r.status_code == 201, r.text
        with db(mongo_db) as d:
            cloud_id = d["servers"].find_one({"account": account, "managed": True})["_id"]
        yield c, account, headers, cloud_id, r.json()["id"]


def server_doc(mongo_db, server_id: str) -> dict:
    with db(mongo_db) as d:
        return d["servers"].find_one({"_id": server_id})


@pytest.mark.parametrize(
    "client_header",
    [None, "ios/0.2.2", "web/0.4.0", "android/1.0.0", "ios/0.3", "ios"],
)
def test_old_clients_never_see_the_cloud_entry(agenda, client_header):
    c, _, headers, cloud_id, self_id = agenda
    if client_header is not None:
        headers = {**headers, "X-Wristcall-Client": client_header}
    servers = c.get("/v1/servers", headers=headers).json()["servers"]
    assert [s["id"] for s in servers] == [self_id]
    r = c.get(f"/v1/servers/{cloud_id}", headers=headers)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert c.get(f"/v1/servers/{self_id}", headers=headers).status_code == 200
    assert c.get("/v1/agents", headers=headers).json() == {"agents": []}
    assert c.get("/v1/account", headers=headers).json()["servers"] == 1


def test_new_clients_see_cloud_agents_from_cloud_mode(agenda, mongo_db):
    c, account, headers, cloud_id, self_id = agenda
    headers = {**headers, **NEW}
    servers = c.get("/v1/servers", headers=headers).json()["servers"]
    assert sorted(s["id"] for s in servers) == sorted([cloud_id, self_id])
    [entry] = [s for s in servers if s["id"] == cloud_id]
    shown = (entry["kind"], entry["url"], entry["name"], entry["linked"])
    assert shown == ("cloud", PUBLIC_URL, "Wristcall Cloud", True)
    [voice_notes] = entry["agents"]
    assert voice_notes["id"].startswith("ag_")
    assert voice_notes == {"id": voice_notes["id"], **VOICE_NOTES}
    assert set(entry) == {"id", "name", "url", "kind", "linked", "agents", "created_at", "updated_at"}
    assert c.get(f"/v1/servers/{cloud_id}", headers=headers).json() == entry

    agents = c.get("/v1/agents", headers=headers).json()["agents"]
    assert agents == [
        {**voice_notes, "server_id": cloud_id, "server_name": "Wristcall Cloud", "server_url": PUBLIC_URL}
    ]
    assert c.get("/v1/account", headers=headers).json()["servers"] == 2

    # A new agent in cloud mode shows up on the next read, without any PUT.
    with db(mongo_db) as d:
        d["cloud_agents"].insert_one(
            {
                "_id": "ag_second", "account": account, "slug": "second", "display_name": "Second", "icon": "star",
                "call_type": "conversation", "color": None, "position": 7, "spec": {"secret": "x"},
                "created_at": time.time(), "updated_at": time.time(),
            }
        )  # fmt: skip
    [entry] = [s for s in c.get("/v1/servers", headers=headers).json()["servers"] if s["id"] == cloud_id]
    assert [a["id"] for a in entry["agents"]] == [voice_notes["id"], "ag_second"]
    assert entry["agents"][1] == {
        "id": "ag_second", "slug": "second", "display_name": "Second", "icon": "star", "call_type": "conversation"
    }  # fmt: skip
    assert len(c.get("/v1/agents", headers=headers).json()["agents"]) == 2
    assert "secret" not in c.get("/v1/agents", headers=headers).text
    # The entry's own `agents` field stays empty: nothing was copied into it.
    assert server_doc(mongo_db, cloud_id)["agents"] == []


def test_web_client_sees_the_entry_with_a_web_token(node_config, mongo_db, fake_verifier, signing_key):  # noqa: F811
    config = replace(node_config, cloud_entry_clients=MINIMUMS)
    for c in served_node(config, mongo_db, fake_verifier, signing_key):
        created = create_account(c, platform="web")
        grant_plan(mongo_db, created["account"], "full")
        c.portal.call(
            functools.partial(provision, c.app.state.store, c.app.state.cloud, created["account"], "full", SETTINGS)
        )
        headers = bearer(created["access_token"])
        assert c.get("/v1/servers", headers=headers).json()["servers"] == []
        seen = c.get("/v1/servers", headers={**headers, "X-Wristcall-Client": "web/0.4.0"}).json()["servers"]
        assert [s["kind"] for s in seen] == ["cloud"]
        older = c.get("/v1/servers", headers={**headers, "X-Wristcall-Client": "web/0.3.9"}).json()["servers"]
        assert older == []


def test_nobody_sees_the_entry_by_default(node_config, mongo_db, fake_verifier, signing_key):  # noqa: F811
    for c in served_node(node_config, mongo_db, fake_verifier, signing_key):
        _, headers = provisioned(c, mongo_db)
        assert c.get("/v1/servers", headers={**headers, "X-Wristcall-Client": "ios/9.9.9"}).json()["servers"] == []


def test_cloud_entry_is_managed_by_cloud(agenda, mongo_db):
    c, _, headers, cloud_id, self_id = agenda
    before = server_doc(mongo_db, cloud_id)
    for h, status, code in ((headers | NEW, 409, "managed_by_cloud"), (headers, 404, "not_found")):
        for r in (
            c.patch(f"/v1/servers/{cloud_id}", json={"name": "x"}, headers=h),
            c.put(f"/v1/servers/{cloud_id}/agents", json={"agents": []}, headers=h),
            c.delete(f"/v1/servers/{cloud_id}", headers=h),
        ):
            assert r.status_code == status, r.text
            assert r.json()["error"] == code
            if status == 409:
                assert r.json()["message"] == "this server is managed by the wristcall Cloud"
        assert server_doc(mongo_db, cloud_id) == before

    # The self-hosted server, as before (with and without the header).
    for h in (headers | NEW, headers):
        r = c.patch(f"/v1/servers/{self_id}", json={"name": "casa"}, headers=h)
        assert r.status_code == 200 and r.json()["name"] == "casa"
        agent = {"id": "ag1", "slug": "a", "display_name": "A", "icon": "waveform", "call_type": "conversation"}
        r = c.put(f"/v1/servers/{self_id}/agents", json={"agents": [agent]}, headers=h)
        assert r.status_code == 200 and r.json() == {"agents": [agent]}
    assert c.delete(f"/v1/servers/{self_id}", headers=headers).status_code == 204
    assert c.get(f"/v1/servers/{self_id}", headers=headers | NEW).status_code == 404


def test_apps_cannot_create_cloud_entries(agenda):
    c, _, headers, _, _ = agenda
    for h in (headers, headers | NEW):
        r = c.post("/v1/servers", json={"name": "n", "url": "https://x.test", "kind": "cloud"}, headers=h)
        assert r.status_code == 422, r.text
        assert r.json() == {"error": "invalid", "message": "kind cloud is managed by the wristcall Cloud"}
    r = c.post("/v1/servers", json={"name": "n", "url": "https://x.test", "kind": "self-hosted"}, headers=headers)
    assert r.status_code == 201 and r.json()["kind"] == "self-hosted"
    r = c.post("/v1/servers", json={"name": "n", "url": "https://y.test"}, headers=headers)
    assert r.status_code == 201 and r.json()["kind"] == "self-hosted"


def test_legacy_cloud_kind_entries_are_unchanged(agenda, mongo_db):
    c, account, headers, _, self_id = agenda
    agent = {"id": "ag1", "slug": "a", "display_name": "A", "icon": "waveform", "call_type": "conversation"}
    legacy = {
        "_id": "srv_legacy000001", "account": account, "name": "Old cloud", "url": "https://old.test", "kind": "cloud",
        "linked": True, "agents": [agent], "created_at": time.time() + 10, "updated_at": time.time() + 10,
    }  # fmt: skip
    with db(mongo_db) as d:
        d["servers"].insert_one(legacy)
    shown = {k: v for k, v in legacy.items() if k not in ("_id", "account")} | {"id": legacy["_id"]}
    for h in (headers, headers | NEW):
        servers = c.get("/v1/servers", headers=h).json()["servers"]
        assert self_id in [s["id"] for s in servers] and servers[-1] == shown
        assert c.get(f"/v1/servers/{legacy['_id']}", headers=h).json() == shown
        assert {**agent, "server_id": legacy["_id"], "server_name": "Old cloud", "server_url": "https://old.test"} in (
            c.get("/v1/agents", headers=h).json()["agents"]
        )
    assert c.get("/v1/account", headers=headers).json()["servers"] == 2

    r = c.patch(f"/v1/servers/{legacy['_id']}", json={"name": "renamed"}, headers=headers)
    assert r.status_code == 200 and r.json()["name"] == "renamed" and r.json()["agents"] == [agent]
    r = c.put(f"/v1/servers/{legacy['_id']}/agents", json={"agents": []}, headers=headers)
    assert r.status_code == 200 and r.json() == {"agents": []}
    assert c.delete(f"/v1/servers/{legacy['_id']}", headers=headers).status_code == 204
    assert server_doc(mongo_db, legacy["_id"]) is None


def test_managed_entry_does_not_take_a_server_slot(node_config, mongo_db, fake_verifier, signing_key):  # noqa: F811
    config = replace(node_config, cloud_entry_clients=MINIMUMS, max_servers=2)
    for c in served_node(config, mongo_db, fake_verifier, signing_key):
        _, headers = provisioned(c, mongo_db)
        assert c.post("/v1/servers", json=SELF_HOSTED, headers=headers).status_code == 201
        r = c.post("/v1/servers", json={"name": "2", "url": "https://2.test"}, headers=headers | NEW)
        assert r.status_code == 201, r.text
        r = c.post("/v1/servers", json={"name": "3", "url": "https://3.test"}, headers=headers)
        assert r.status_code == 403 and r.json()["error"] == "limit"
        assert c.get("/v1/account", headers=headers | NEW).json()["servers"] == 3


def test_cors_allows_the_client_header(node_config, mongo_db, fake_verifier, signing_key):  # noqa: F811
    config = replace(node_config, cors_origins=("https://app.test",))
    for c in served_node(config, mongo_db, fake_verifier, signing_key):
        r = c.options(
            "/v1/servers",
            headers={
                "Origin": "https://app.test",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-wristcall-client",
            },
        )
        assert r.status_code == 200, r.text
        assert "x-wristcall-client" in r.headers["access-control-allow-headers"].lower()
