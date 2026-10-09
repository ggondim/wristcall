import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from conftest import fake_config
from wristcall.app import create_app
from wristcall.history import CallLog
from wristcall.history_codec import new_key
from wristcall.storage import CallRecord, open_sqlite_storage
from wristcall.users import UserService

DAY = 86_400.0
T0 = 1791504000.0  # 2026-10-09T00:00:00Z


def run(coro):
    return asyncio.run(coro)


def h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make(cfg=None):
    return TestClient(create_app(cfg or fake_config(), storage=open_sqlite_storage(":memory:")))


@pytest.fixture
def client():
    with make() as c:
        yield c


def user(client, handle="owner"):
    st = client.app.state.storage
    return run(st.users.by_handle(handle)) or run(UserService(st).create(handle))


def api_token(client, handle="owner") -> str:
    return run(UserService(client.app.state.storage).issue_token(user(client, handle).id, "test"))[1]


def device_token(client) -> str:
    code = run(client.app.state.pairing.create_code(user(client).id)).code
    return client.post("/v1/pair", json={"code": code, "device_name": "Watch"}).json()["token"]


def add_call(client, call_id, at, texts, *, handle="owner", agent=None, call_type="conversation", status="ended", **over):
    """A finished call written through the history (so sealed and indexed like a real one)."""
    owner = user(client, handle)
    agent_id = agent or run(client.app.state.agents.list(owner.id))[0].id
    history = client.app.state.history
    record = run(history.storage.calls.create(CallRecord(
        id=call_id, user_id=owner.id, agent_id=agent_id, device_id=None, call_type=call_type, status=status,
        created_at=at, updated_at=at, ended_at=at + 5, finished_at=at + 6, agent_slug="default", agent_name="Test",
        **over,
    )))
    log = CallLog(history.storage, history.codec, record, now=lambda: at + 1)
    for role, text in texts:
        run(log.add(role, text))
    return record


@pytest.fixture
def filled(client):
    """owner: c_1 (t0, milk), c_2 (t0 + 1 day, bread + agent answer), c_3 (t0 + 2 days, one-shot failed).
    other: c_9 (milk)."""
    add_call(client, "c_1", T0, [("user", "Comprar leite amanhã"), ("agent", "Anotado.")])
    add_call(client, "c_2", T0 + DAY, [("user", "pão e café"), ("agent", "Café sem açúcar?")])
    add_call(
        client, "c_3", T0 + 2 * DAY, [("user", "ideia de app")], call_type="one-shot", status="failed",
        error="delivery_failed", attempts=3, last_http_status=500,
    )
    add_call(client, "c_9", T0, [("user", "leite")], handle="other", agent="ag_000000000000")
    return client


def ids(client, token, **params):
    r = client.get("/v1/calls", headers=h(token), params=params)
    assert r.status_code == 200, r.text
    return [c["id"] for c in r.json()["calls"]]


def test_list_is_newest_first_with_the_entries(filled):
    t = api_token(filled)
    body = filled.get("/v1/calls", headers=h(t)).json()
    assert [c["id"] for c in body["calls"]] == ["c_3", "c_2", "c_1"] and body["next_before"] is None
    c1 = body["calls"][2]
    assert [(e["role"], e["text"]) for e in c1["entries"]] == [("user", "Comprar leite amanhã"), ("agent", "Anotado.")]
    assert c1["text"] == "Comprar leite amanhã" and c1["agent"]["slug"] == "default"


def test_search_ignores_accents_and_case_and_finds_answers(filled):
    t = api_token(filled)
    assert ids(filled, t, q="LEITE") == ["c_1"]  # not the other user's
    assert ids(filled, t, q="acucar") == ["c_2"]  # the agent's answer
    assert ids(filled, t, q="pão café") == ["c_2"]
    assert ids(filled, t, q="pão açúcar") == ["c_2"]  # the user's words and the agent's, one call
    assert ids(filled, t, q="pão leite") == []  # every word in the same call
    assert ids(filled, t, q="leit") == []  # whole words only
    assert ids(filled, t, q='" OR * NEAR(') == []  # FTS5 syntax is just text
    assert ids(filled, t, q="") == ["c_3", "c_2", "c_1"]  # empty search: no filter


def test_filters_by_agent_and_period(filled):
    t = api_token(filled)
    agent = filled.get("/v1/agents", headers=h(t)).json()["agents"][0]
    assert ids(filled, t, agent="default") == ["c_3", "c_2", "c_1"]
    assert ids(filled, t, agent=agent["id"], since="2026-10-10") == ["c_3", "c_2"]
    assert ids(filled, t, until="2026-10-10T00:00:00Z") == ["c_1"]
    assert ids(filled, t, since=str(T0 + DAY), until=str(T0 + 2 * DAY)) == ["c_2"]
    assert ids(filled, t, agent="ag_0123456789ab") == []  # a deleted agent's id still filters
    assert filled.get("/v1/calls", headers=h(t), params={"agent": "nobody"}).status_code == 404
    bad = filled.get("/v1/calls", headers=h(t), params={"since": "yesterday"})
    assert bad.status_code == 422 and bad.json()["error"] == "invalid"


def test_pages_with_before(filled):
    t = api_token(filled)
    first = filled.get("/v1/calls", headers=h(t), params={"limit": 2}).json()
    assert [c["id"] for c in first["calls"]] == ["c_3", "c_2"] and first["next_before"].endswith(":c_2")
    filled.delete("/v1/calls/c_2", headers=h(t))  # the cursor survives its call
    assert ids(filled, t, limit=2, before=first["next_before"]) == ["c_1"]
    assert filled.get("/v1/calls", headers=h(t), params={"limit": 101}).status_code == 422
    bad = filled.get("/v1/calls", headers=h(t), params={"before": "c_2"})
    assert bad.status_code == 422 and "next_before" in bad.json()["message"]


def test_history_needs_the_owner_api_token(filled):
    device = device_token(filled)
    for method, path in (
        ("get", "/v1/calls"), ("delete", "/v1/calls?all=true"), ("get", "/v1/calls/export"),
        ("delete", "/v1/calls/c_1"), ("post", "/v1/calls/c_3/redeliver"),
    ):
        r = filled.request(method.upper(), path, headers=h(device))
        assert r.status_code == 403 and r.json()["error"] == "forbidden", path
        assert filled.request(method.upper(), path).status_code == 401
    # Reading one call stays open to the device (it asks how its call went).
    assert filled.get("/v1/calls/c_1", headers=h(device)).json()["entries"][0]["text"] == "Comprar leite amanhã"
    assert filled.get("/v1/calls/c_9", headers=h(device)).status_code == 404


def test_delete_one_by_agent_or_all(filled):
    t, other = api_token(filled), api_token(filled, "other")
    assert filled.delete("/v1/calls/c_9", headers=h(t)).status_code == 404  # not yours
    assert filled.delete("/v1/calls/c_2", headers=h(t)).status_code == 204
    assert filled.delete("/v1/calls/c_2", headers=h(t)).status_code == 404
    assert ids(filled, t, q="pao") == []
    assert filled.delete("/v1/calls", headers=h(t)).status_code == 422  # neither agent nor all
    assert filled.delete("/v1/calls?all=true&agent=default", headers=h(t)).status_code == 422
    assert filled.delete("/v1/calls?agent=default", headers=h(t)).json() == {"deleted": 2}
    assert ids(filled, t) == []
    assert filled.delete("/v1/calls?all=true", headers=h(other)).json() == {"deleted": 1}


def test_export_markdown(filled):
    t = api_token(filled)
    r = filled.get("/v1/calls/export", headers=h(t), params={"format": "md", "until": "2026-10-11"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert r.headers["content-disposition"].startswith('attachment; filename="wristcall-history-')
    text = r.text
    assert text.startswith("# wristcall history\n")
    assert "## 2026-10-10 00:00 | Test | conversation | ended" in text
    assert "**You:** pão e café" in text and "**Agent:** Café sem açúcar?" in text
    assert text.index("2026-10-10 00:00") < text.index("2026-10-09 00:00")  # newest first
    assert "ideia de app" not in text and "leite" in text.lower()


def test_export_json_round_trips_the_detail_view(filled):
    t = api_token(filled)
    r = filled.get("/v1/calls/export", headers=h(t), params={"format": "json", "agent": "default"})
    assert r.headers["content-type"] == "application/json"
    body = json.loads(r.text)
    assert body["version"] == 1 and body["exported_at"].endswith("Z")
    assert [c["id"] for c in body["calls"]] == ["c_3", "c_2", "c_1"]
    failed = body["calls"][0]
    assert (failed["status"], failed["error"], failed["attempts"], failed["last_http_status"]) == (
        "failed", "delivery_failed", 3, 500
    )
    assert body["calls"][2] == filled.get("/v1/calls/c_1", headers=h(t)).json()


def test_export_rejects_unknown_format_and_streams_past_one_page(client):
    t = api_token(client)
    assert client.get("/v1/calls/export", headers=h(t), params={"format": "csv"}).status_code == 422
    for i in range(130):
        add_call(client, f"c_{i:03d}", T0 + i, [("user", f"nota {i}")])
    body = json.loads(client.get("/v1/calls/export", headers=h(t), params={"format": "json"}).text)
    assert len(body["calls"]) == 130 and body["calls"][-1]["id"] == "c_000"
    empty = json.loads(client.get("/v1/calls/export", headers=h(t), params={"format": "json", "since": "2030-01-01"}).text)
    assert empty["calls"] == []


def test_encrypted_history_is_searchable_and_unreadable_on_disk():
    cfg = fake_config()
    cfg.history.encryption_key = new_key()
    with make(cfg) as c:
        add_call(c, "c_1", T0, [("user", "senha do cofre é azul")])
        t = api_token(c)
        assert ids(c, t, q="cofre azul") == ["c_1"]
        assert c.get("/v1/calls/c_1", headers=h(t)).json()["entries"][0]["text"] == "senha do cofre é azul"
        db = c.app.state.storage.db
        stored = db.query("SELECT text, sealed FROM call_entries")[0]
        assert stored["sealed"] == 1 and "cofre" not in stored["text"]
        assert "cofre" not in db.query("SELECT terms FROM history_fts")[0]["terms"]
