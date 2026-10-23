"""Cloud mode against the real Wristcall Cloud (`wristcall_cloud`) on a real MongoDB: the node, the Cloud and the
database are the ones that run in production, only the voice providers and the agent's LLM are fakes.

The Cloud runs with uvicorn in a thread on a free loopback port (its own lifespan, indexes and Mongo client, on its own
loop); the node is registered with the Cloud's own `NodeStore` as `https://node.test`, the URL the users' tokens are
made for; the server runs in process (TestClient) and reaches the Cloud over real HTTP. The users' tokens come from
the Cloud's `POST /v1/route`, for accounts made through its own API.

Skipped unless WRISTCALL_TEST_CLOUD_MONGO_URL names a MongoDB (8) and `wristcall_cloud` is importable (CI job
`cloud-node`; locally `.venv/bin/pip install -e ../cloud`). Every run uses a database of its own and drops it.
"""

import asyncio
import base64
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx

MONGO_URL = os.environ.get("WRISTCALL_TEST_CLOUD_MONGO_URL")
if not MONGO_URL:
    pytest.skip("WRISTCALL_TEST_CLOUD_MONGO_URL is not set", allow_module_level=True)
pytest.importorskip("wristcall_cloud")

import uvicorn  # noqa: E402
from conftest import fake_resolver, silence, tone  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pymongo import AsyncMongoClient, MongoClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402
from wristcall_cloud.app import create_app as create_cloud_app  # noqa: E402
from wristcall_cloud.cloud_store import CloudModeStore  # noqa: E402
from wristcall_cloud.config import CloudConfig  # noqa: E402
from wristcall_cloud.nodes import NodeStore  # noqa: E402

from wristcall.app import create_app  # noqa: E402
from wristcall.audio import wav_to_pcm16  # noqa: E402
from wristcall.config import parse_config  # noqa: E402
from wristcall.history import aad_scoped  # noqa: E402
from wristcall.history_codec import HistoryCodec, new_key, parse_key  # noqa: E402

pytestmark = pytest.mark.cloud_real

NODE_URL = "https://node.test"
NODE_ID = "home"
LLM_URL = "https://llm.example.test/v1/chat/completions"
LLM = {"type": "openai_chat", "base_url": "https://llm.example.test/v1", "model": "m", "api_key": "sk-agent-secret"}
REPLY = "Hello there, zephyr."
# The operator's voices: fakes. `stt-default` is the STT the Cloud provisions "Voice notes" with (full plan).
PROVIDERS = {
    "stt": {"type": "fake_stt", "text": "hi"},
    "tts": {"type": "tone_tts", "sample_rate": 16000},
    "stt-default": {"type": "fake_stt", "text": "buy oat milk"},
}
TALK = {
    "slug": "talk", "display_name": "Talk", "stt": {"provider": "stt"}, "tts": {"provider": "tts"}, "action": LLM,
    "vad": {"type": "energy"},
}
START = {"type": "session.start", "protocol": 1, "audio_in": {"codec": "pcm16", "sample_rate": 16000, "channels": 1}}
PUBLIC_HOSTS = {"llm.example.test": ["93.184.216.34"]}


# --- the real Cloud -------------------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def new_signing_pem() -> bytes:
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@dataclass
class RealCloud:
    url: str
    node_secret: str
    http: httpx.Client  # the apps' side of the Cloud API
    db: Any  # a synchronous handle on the Cloud's database (its async client belongs to the uvicorn loop)


def serve_in_thread(app: Any, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, name="real-cloud", daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            # Whatever ran of the lifespan (indexes, a Mongo client) is stopped before the test gives up.
            server.should_exit = True
            thread.join(15)
            raise CloudDidNotStart("the Cloud did not start")
        time.sleep(0.02)
    return server, thread


class CloudDidNotStart(AssertionError):
    pass


async def register_node(database: str) -> str:
    """Registers `home` at NODE_URL with the Cloud's node store (as `admin nodes add` does); returns its secret."""
    client: AsyncMongoClient = AsyncMongoClient(MONGO_URL, serverSelectionTimeoutMS=5000, tz_aware=True)
    try:
        _, secret = await NodeStore(CloudModeStore(client[database])).add(
            NODE_ID, url=NODE_URL, region="gru", kind="always_on", capabilities=["operator_stt", "operator_tts"]
        )
    finally:
        await client.close()
    return secret


def start_cloud(database: str) -> tuple[str, uvicorn.Server, threading.Thread]:
    """Starts the Cloud on a free port; a port taken between `free_port()` and the bind is tried once more."""
    for attempt in (1, 2):
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        config = CloudConfig(
            mongo_url=MONGO_URL, database=database, issuer="https://auth.test", clients={"ios": "client-ios"},
            public_url=url, signing_key_pem=new_signing_pem(), push_fake=True,
        )
        try:
            server, thread = serve_in_thread(create_cloud_app(config), port)
        except CloudDidNotStart:
            if attempt == 2:
                raise
            continue
        return url, server, thread
    raise AssertionError("unreachable")  # pragma: no cover


@pytest.fixture(scope="module")
def real_cloud() -> Iterator[RealCloud]:
    database = f"test_node_{uuid.uuid4().hex}"
    sync: MongoClient = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000, tz_aware=True)
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    try:  # the database is dropped whatever fails, even a Cloud that did not finish starting
        url, server, thread = start_cloud(database)
        secret = asyncio.run(register_node(database))
        with httpx.Client(base_url=url, timeout=10) as http:
            yield RealCloud(url, secret, http, sync[database])
    finally:
        if server is not None and thread is not None:
            server.should_exit = True
            thread.join(15)
        sync.drop_database(database)
        sync.close()


# --- the node -------------------------------------------------------------------------------------------------------


@dataclass
class Node:
    client: TestClient
    history_key: str
    beats: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture(scope="module")
def node(real_cloud: RealCloud) -> Iterator[Node]:
    history_key = new_key()
    config = parse_config(
        {
            "server": {"public_url": NODE_URL, "mode": "cloud"},
            "providers": PROVIDERS,
            "central_account": {"issuer": real_cloud.url, "audience": [NODE_URL]},
            "history": {"encryption_key": history_key},
            "cloud_node": {
                "cloud_url": real_cloud.url, "node_id": NODE_ID, "node_secret": real_cloud.node_secret,
                "seal_key": new_key(), "heartbeat_s": 0.3,
            },
        },
        {},
    )
    beats: list[dict[str, Any]] = []

    async def record(request: httpx.Request) -> None:
        if request.url.path == "/v1/node/heartbeat":
            beats.append(json.loads(request.content))

    # No kept-alive connections: they would belong to the TestClient's loop, closed with it, and could not be closed
    # after it.
    cloud_http = httpx.AsyncClient(event_hooks={"request": [record]}, limits=httpx.Limits(max_keepalive_connections=0))
    sse = f'data: {{"choices": [{{"delta": {{"content": "{REPLY}"}}}}]}}\n\ndata: [DONE]\n\n'
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="testserver").pass_through()
        mock.route(host="127.0.0.1").pass_through()
        mock.post(LLM_URL).mock(
            return_value=httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        )
        app = create_app(
            config, cloud_http=cloud_http, resolver=fake_resolver(PUBLIC_HOSTS),
            cloud_stop_s=5.0, background_grace_s=2.0, outbox_max_delay_s=0.2,
        )
        try:
            with TestClient(app) as client:
                yield Node(client, history_key, beats)
        finally:
            asyncio.run(cloud_http.aclose())  # the TestClient is gone: nothing else holds its connections


# --- helpers --------------------------------------------------------------------------------------------------------


def wait_for(check: Callable[[], Any], timeout_s: float = 10.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} not met in time")
        time.sleep(0.05)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def new_account_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()


def create_account(cloud: RealCloud, platform: str = "ios") -> tuple[dict[str, Any], str]:
    """A silent account through the Cloud's API: (session, account key)."""
    key = new_account_key()
    r = cloud.http.post("/v1/accounts", json={"account_key": key, "device": {"name": "phone", "platform": platform}})
    assert r.status_code == 201, r.text
    return r.json(), key


def enroll(cloud: RealCloud, key: str, platform: str) -> dict[str, Any]:
    r = cloud.http.post("/v1/devices", json={"account_key": key, "device": {"name": "watch", "platform": platform}})
    assert r.status_code == 201, r.text
    return r.json()


def grant_plan(cloud: RealCloud, account: str, plan: str = "full") -> None:
    cloud.db["entitlements"].replace_one(
        {"_id": account},
        {"_id": account, "plan": plan, "source": "beta", "granted_at": time.time(), "expires_at": None},
        upsert=True,
    )


def route(cloud: RealCloud, session: dict[str, Any], **body: Any) -> httpx.Response:
    return cloud.http.post("/v1/route", json=body, headers=bearer(session["access_token"]))


def node_token(cloud: RealCloud, session: dict[str, Any], **body: Any) -> str:
    """The per-server token the Cloud hands the app for this node (`POST /v1/route`)."""
    r = route(cloud, session, **body)
    assert r.status_code == 200, r.text
    assert r.json()["node"]["id"] == NODE_ID and r.json()["node"]["url"] == NODE_URL
    return r.json()["token"]


def closed_with(ws: Any) -> int:
    with pytest.raises(WebSocketDisconnect) as e:
        while True:
            ws.receive_text()
    return e.value.code


def read_until_agent_end(ws: Any) -> list[str]:
    events = []
    while True:
        m = ws.receive()
        if m.get("bytes"):
            events.append("audio")
            continue
        msg = json.loads(m["text"])
        events.append(msg["type"])
        if msg["type"] == "turn.agent_end":
            return events


def opened_entries(cloud: RealCloud, node: Node, account: str, call_id: str) -> list[tuple[str, str]]:
    """The call's entries as stored in Mongo, each one sealed, opened with the node's history key."""
    codec = HistoryCodec(parse_key(node.history_key)).for_account(account)
    out = []
    for e in cloud.db["cloud_call_entries"].find({"call": call_id}).sort("seq", 1):
        assert e["sealed"] is True and e["account"] == account
        out.append((e["role"], codec.open(e["text"], True, aad_scoped(account, call_id, e["seq"]))))
    return out


def month_used(cloud: RealCloud, account: str) -> float:
    month = time.strftime("%Y-%m", time.gmtime())
    doc = cloud.db["usage"].find_one({"_id": f"{account}:{month}"})
    return (doc or {}).get("total_s", 0)


# --- the test -------------------------------------------------------------------------------------------------------


def test_real_cloud_end_to_end(real_cloud: RealCloud, node: Node, fixtures_dir):
    cloud, c = real_cloud, node.client
    phone, key = create_account(cloud, "ios")
    account = phone["account"]
    watch = enroll(cloud, key, "watchos")
    grant_plan(cloud, account)

    # The node is ready once the real Cloud took a heartbeat; only then does the route pick it.
    wait_for(lambda: c.get("/v1/health").status_code == 200, what="node ready")
    manage = node_token(cloud, phone, purpose="manage")  # also provisions "Voice notes"
    me = c.get("/v1/me", headers=bearer(manage))
    assert me.status_code == 200, me.text
    assert me.json()["plan"]["plan"] == "full"
    assert [a["slug"] for a in me.json()["agents"]] == ["voice-notes"]

    # An agent made through the node: its secrets reach Mongo sealed.
    r = c.post("/v1/agents", json=TALK, headers=bearer(manage))
    assert r.status_code == 201, r.text
    talk = r.json()
    stored = cloud.db["cloud_agents"].find_one({"_id": talk["id"]})
    assert stored["account"] == account and stored["slug"] == "talk"
    as_text = json.dumps(stored, default=str)
    assert "sk-agent-secret" not in as_text and "llm.example.test" not in as_text
    assert stored["spec"]["action"]["sealed"].startswith("v1.")
    assert stored["spec"]["stt"] == {"provider": "stt"} and stored["spec"]["tts"] == {"provider": "tts"}

    # A conversation from the watch, with a token routed for this very call.
    used_before = month_used(cloud, account)
    call_token = node_token(cloud, watch, purpose="call", agent=talk["id"])
    with c.websocket_connect("/v1/call", headers=bearer(call_token)) as ws:
        ws.send_json({**START, "agent": "talk"})
        ready = ws.receive_json()
        assert ready["type"] == "session.ready", ready
        call_id = ready["call_id"]
        opened = cloud.db["cloud_calls"].find_one({"_id": call_id})
        assert opened["open"] is True and opened["node"] == NODE_ID and opened["account"] == account
        ws.send_bytes(tone(500) + silence(900))
        events = read_until_agent_end(ws)
        assert events[:3] == ["turn.user_end", "transcript", "turn.agent_start"] and "audio" in events
        wait_for(lambda: any(call_id in b["active_call_ids"] for b in node.beats), what="call in a heartbeat")
        time.sleep(1.1)  # at least one billed second
        ws.send_json({"type": "session.end"})
        assert closed_with(ws) == 1000

    # The finish reached the Cloud: the call is closed by the node, with its seconds, and billed.
    done = wait_for(
        lambda: (d := cloud.db["cloud_calls"].find_one({"_id": call_id})) and "open" not in d and d, what="finish"
    )
    assert done["status"] == "ended" and done.get("closed_by") == "node", done
    assert 1 <= done["seconds"] <= 30
    assert month_used(cloud, account) >= used_before + done["seconds"]
    # The entries are sealed in Mongo (never in clear) and open with the node's history key.
    wait_for(lambda: cloud.db["cloud_call_entries"].count_documents({"call": call_id}) == 2, what="entries")
    for e in cloud.db["cloud_call_entries"].find({"call": call_id}):
        assert "zephyr" not in e["text"].lower() and e["text"] != "hi"
    assert opened_entries(cloud, node, account, call_id) == [("user", "hi"), ("agent", REPLY)]
    # The next heartbeats hold no call.
    seen = len(node.beats)
    wait_for(lambda: len(node.beats) > seen + 1, what="later heartbeats")
    assert node.beats[-1]["active_call_ids"] == []
    assert node.beats[-1]["active_by_type"] == {"conversation": 0, "one_way": 0}
    assert node.beats[-1]["capacity"] == {"conversation": 2, "one_way": 2}
    registered = cloud.db["nodes"].find_one({"_id": NODE_ID})
    assert registered["active_by_type"] == {"conversation": 0, "one_way": 0}
    assert registered["capacity"] == {"conversation": 2, "one_way": 2}

    # "Voice notes", as the Cloud provisioned it: a one-shot call kept in the history only.
    voice_notes = next(a for a in me.json()["agents"] if a["slug"] == "voice-notes")
    pcm, rate = wav_to_pcm16((fixtures_dir / "speech_pt_16k.wav").read_bytes())
    assert rate == 16000
    note_token = node_token(cloud, watch, purpose="call", agent=voice_notes["id"])
    with c.websocket_connect("/v1/call", headers=bearer(note_token)) as ws:
        ws.send_json({**START, "agent": "voice-notes"})
        ready = ws.receive_json()
        assert ready["type"] == "session.ready", ready
        note_id = ready["call_id"]
        ws.send_bytes(pcm[: 16000 * 2 * 2])
        ws.send_json({"type": "session.end"})
        closed_with(ws)
    note = wait_for(
        lambda: (d := cloud.db["cloud_calls"].find_one({"_id": note_id})) and d["status"] == "delivered" and d,
        what="voice note delivered",
    )
    assert note["delivery"] == "history" and note["call_type"] == "one-shot" and "open" not in note
    wait_for(lambda: cloud.db["cloud_call_entries"].count_documents({"call": note_id}) == 1, what="note entry")
    assert opened_entries(cloud, node, account, note_id) == [("user", "buy oat milk")]

    # Search through the node finds a word of each call; the export holds both.
    found = c.get("/v1/calls", params={"q": "zephyr"}, headers=bearer(manage))
    assert found.status_code == 200, found.text
    assert [x["id"] for x in found.json()["calls"]] == [call_id]
    found = c.get("/v1/calls", params={"q": "oat"}, headers=bearer(manage))
    assert [x["id"] for x in found.json()["calls"]] == [note_id]
    exported = c.get("/v1/calls/export", params={"format": "json"}, headers=bearer(manage))
    assert exported.status_code == 200, exported.text
    calls = {x["id"]: x for x in exported.json()["calls"]}
    assert set(calls) == {call_id, note_id}
    assert "Hello there, zephyr." in json.dumps(calls[call_id])
    md = c.get("/v1/calls/export", params={"format": "md"}, headers=bearer(manage))
    assert md.status_code == 200 and "buy oat milk" in md.text

    # Without a plan: the node answers 402 plan_required (REST) and refuses the call.
    other, _ = create_account(cloud, "ios")
    grant_plan(cloud, other["account"])
    lapsed = node_token(cloud, other, purpose="manage")  # routed with a plan, which then lapses
    cloud.db["entitlements"].delete_one({"_id": other["account"]})
    r = c.post("/v1/agents", json={**TALK, "slug": "talk-2"}, headers=bearer(lapsed))
    assert r.status_code == 402, r.text
    assert r.json()["error"] == "plan_required"
    assert c.get("/v1/me", headers=bearer(lapsed)).json()["plan"] is None
    assert c.get("/v1/calls", headers=bearer(lapsed)).status_code == 200  # a lapsed plan still reads its history
    with c.websocket_connect("/v1/call", headers=bearer(lapsed)) as ws:
        ws.send_json({**START, "agent": "voice-notes"})
        msg = ws.receive_json()
        assert (msg["type"], msg["code"], msg["fatal"]) == ("error", "plan_required", True)
    # ...and the Cloud no longer routes a call for it.
    own_notes = cloud.db["cloud_agents"].find_one({"account": other["account"], "slug": "voice-notes"})
    r = route(cloud, other, purpose="call", agent=own_notes["_id"])
    assert r.status_code == 402 and r.json()["error"] == "plan_required"


def test_real_cloud_takes_a_heartbeat_with_stats(real_cloud: RealCloud, node: Node):
    """The node's counters (`stats`, server 0.11.0) ride in its heartbeat: the real Cloud takes the beat with them,
    and the node then takes what it sent off its counters."""
    c = node.client
    wait_for(lambda: c.get("/v1/health").status_code == 200, what="node ready")
    stats = c.app.state.cloud_node.stats
    seen_before = real_cloud.db["nodes"].find_one({"_id": NODE_ID})["last_seen_at"]

    def count() -> None:  # on the app's own loop, like the calls that count
        stats.busy("one_way")
        stats.first_audio(480)
        stats.provider_time("stt", "stt-default", 900)
        stats.provider_time("tts", "user", 300)
        stats.provider_failed("tts")
        stats.outbox_dropped()

    c.portal.call(count)
    beat = wait_for(
        lambda: next((b for b in node.beats if b.get("stats", {}).get("busy", {}).get("one_way", 0) >= 1), None),
        what="a heartbeat with the counters",
    )
    sent = beat["stats"]
    assert set(sent) == {
        "calls", "busy", "not_ready", "peak", "first_audio_ms", "stt_ms", "tts_ms", "provider_failures",
        "cloud_unreachable_s", "outbox_dropped",
    }
    assert sent["first_audio_ms"]["500"] >= 1 and sent["stt_ms"]["stt-default"]["1000"] >= 1
    assert sent["tts_ms"]["user"]["500"] >= 1 and sent["provider_failures"]["tts"] >= 1 and sent["outbox_dropped"] >= 1
    # Accepted: the node took the counters off, and the Cloud saw the node again.
    wait_for(lambda: c.portal.call(stats.snapshot)["busy"]["one_way"] == 0, what="counters taken off")
    wait_for(
        lambda: real_cloud.db["nodes"].find_one({"_id": NODE_ID})["last_seen_at"] > seen_before, what="node seen again"
    )
