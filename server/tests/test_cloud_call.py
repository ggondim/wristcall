"""Cloud mode: the WebSocket /v1/call opened, cut and finished through the Cloud (Task 9), plus the fixes left by the
reviews of Tasks 4, 8a and 8b."""

import asyncio
import json
import time

import httpx
import pytest
import respx
from cloud_app_fixtures import bearer, cloud_app, cloud_config, new_fake, wait_ready
from conftest import fake_config, silence, tone
from fake_cloud import FULL_PLAN, FakeCloud
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wristcall.admission import CallSlots
from wristcall.app import create_app
from wristcall.audio import wav_to_pcm16
from wristcall.cloud_node.auth import CloudAuthenticator
from wristcall.cloud_node.calls import CallRefused, OpenedCall, call_deadline, check_plan, open_call
from wristcall.cloud_node.node import CloudNode
from wristcall.cloud_node.proof import UserProof
from wristcall.history import aad_scoped
from wristcall.history_api import _ClosingStream
from wristcall.history_codec import HistoryCodec
from wristcall.oidc import OidcVerifier
from wristcall.storage import AgentRecord, CloudUnavailable, open_sqlite_storage

ACCOUNT = "acc-1"
WATCH = "dev-watch"
CUSTOM_PLAN = {"plan": "custom", "source": "beta", "expires_at": None}
PUSH_KEY = "wc_push_" + "k" * 43
START = {"type": "session.start", "protocol": 1, "audio_in": {"codec": "pcm16", "sample_rate": 16000, "channels": 1}}
LLM_URL = "https://llm.example.test/v1/chat/completions"
LLM = {"type": "openai_chat", "base_url": "https://llm.example.test/v1", "model": "m", "api_key": "sk-agent-secret"}
# The operator's voices in the test YAML: fake ones, and the STT a node serving the full plan names `stt-default` (D19).
PROVIDERS = {
    "stt": {"type": "fake_stt", "text": "hi"},
    "tts": {"type": "tone_tts", "sample_rate": 16000},
    "stt-default": {"type": "fake_stt", "text": "buy oat milk"},
}
TALK = {"slug": "talk", "display_name": "Talk", "stt": {"provider": "stt"}, "tts": {"provider": "tts"}, "action": LLM,
        "vad": {"type": "energy"}}
# The "Voice notes" agent as the Cloud provisions it (D19): spec not sealed, one-shot, no tts.
VOICE_NOTES_SPEC = {"language": "en", "stt": {"provider": "stt-default"}, "action": None, "retention_days": None}
VOICE_NOTES_ID = "ag_0123456789ab"

PLAN_REQUIRED = ("plan_required", None, "This account has no Wristcall Cloud plan.")
QUOTA_EXCEEDED = ("quota_exceeded", None, "You've used this month's call minutes.")
CLOUD_UNREACHABLE = ("agent_unavailable", "cloud_unreachable", "Wristcall Cloud is unavailable; try again.")
NEEDS_VOICE_URL = (
    "agent_unavailable", "needs_voice_url", "This agent needs your own transcription and voice addresses.",
)
TOO_MANY_CALLS = ("busy", "too_many_calls", "Another call is still open on this account.")
BUSY = ("busy", None, "This server is busy; try again in a minute.")


@pytest.fixture
def fake() -> FakeCloud:
    cloud = new_fake()
    cloud.plans[ACCOUNT] = FULL_PLAN
    return cloud


@pytest.fixture
def llm():
    """The agent's own responder (an OpenAI-compatible endpoint), answering every turn with one sentence."""
    sse = 'data: {"choices": [{"delta": {"content": "Hello there."}}]}\n\ndata: [DONE]\n\n'
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="testserver").pass_through()
        yield mock.post(LLM_URL).mock(
            return_value=httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        )


def call_app(fake: FakeCloud, **kwargs):
    return cloud_app(fake, cloud_config(providers=PROVIDERS), **kwargs)


def watch(fake: FakeCloud, account: str = ACCOUNT, **claims) -> dict[str, str]:
    return bearer(fake, account, WATCH, "watchos", **claims)


def ios(fake: FakeCloud, account: str = ACCOUNT) -> dict[str, str]:
    return bearer(fake, account, "dev-ios", "ios")


def seed_voice_notes(fake: FakeCloud, account: str = ACCOUNT, spec: dict | None = None) -> str:
    now = time.time()
    fake.agents[VOICE_NOTES_ID] = {
        "id": VOICE_NOTES_ID, "slug": "voice-notes", "display_name": "Voice notes", "icon": "mic", "color": None,
        "call_type": "one-shot", "position": 0, "spec": dict(spec or VOICE_NOTES_SPEC), "created_at": now,
        "updated_at": now, "account": account,
    }
    return VOICE_NOTES_ID


def create_talk(c: TestClient, fake: FakeCloud) -> dict:
    r = c.post("/v1/agents", json=TALK, headers=ios(fake))
    assert r.status_code == 201, r.text
    return r.json()


def wait_for(check, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


def read_until_agent_end(ws) -> list[str]:
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


def closed_with(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as e:
        while True:
            ws.receive_text()
    return e.value.code


def requests_to(fake: FakeCloud, method: str, suffix: str, since: int = 0) -> list[int]:
    return [i for i, r in enumerate(fake.requests) if i >= since and r["method"] == method
            and r["path"].endswith(suffix)]


def opened_calls(fake: FakeCloud) -> list[dict]:
    return [c for c in fake.calls.values() if c["request_id"] is not None]


def sealed_texts(fake: FakeCloud, call_id: str) -> list[tuple[str, str]]:
    codec = HistoryCodec(cloud_config().history.key()).for_account(ACCOUNT)
    entries = fake.entries.get(call_id, {})
    out = []
    for seq in sorted(entries):
        e = entries[seq]
        assert e["sealed"] is True
        out.append((e["role"], codec.open(e["text"], True, aad_scoped(ACCOUNT, call_id, seq))))
    return out


def speech(fixtures_dir) -> bytes:
    pcm, rate = wav_to_pcm16((fixtures_dir / "speech_pt_16k.wav").read_bytes())
    assert rate == 16000
    return pcm


# --- calls -----------------------------------------------------------------------------------------------------------


def test_conversation_round_trip(fake: FakeCloud, llm):
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        agent = create_talk(c, fake)
        with c.websocket_connect("/v1/call", headers=watch(fake)) as ws:
            ws.send_json({**START, "agent": "talk"})
            ready = ws.receive_json()
            assert ready["type"] == "session.ready", ready
            call_id = ready["call_id"]
            assert call_id in fake.calls and fake.calls[call_id]["open"]
            assert ready["agent"]["id"] == agent["id"]
            stored = fake.calls[call_id]
            assert (stored["device_id"], stored["platform"], stored["call_type"]) == (WATCH, "watchos", "conversation")
            assert stored["delivery"] is None
            ws.send_bytes(tone(500) + silence(900))
            events = read_until_agent_end(ws)
            assert events[:3] == ["turn.user_end", "transcript", "turn.agent_start"] and "audio" in events
            node = c.app.state.cloud_node
            wait_for(lambda: any(call_id in b["active_call_ids"] for b in fake.heartbeats))
            ws.send_json({"type": "session.end"})
            assert closed_with(ws) == 1000
        wait_for(lambda: not fake.calls[call_id]["open"])
        stored = fake.calls[call_id]
        assert stored["status"] == "ended" and 1 <= stored["seconds"] <= 10
        assert stored["ended_at"] is not None
        wait_for(lambda: len(fake.entries.get(call_id, {})) == 2)
        assert sealed_texts(fake, call_id) == [("user", "hi"), ("agent", "Hello there.")]
        # The finish was accepted: the next heartbeat no longer holds the call.
        wait_for(lambda: node.registry.ids() == [])
        assert node.registry.grant(call_id) is None
        seen = len(fake.heartbeats)
        wait_for(lambda: len(fake.heartbeats) > seen + 1)
        assert call_id not in fake.heartbeats[-1]["active_call_ids"]
        assert c.app.state.call_slots.active() == {"conversation": 0, "one_way": 0}
        # D1: the Cloud was asked what the account may do, then to open the call.
        assert requests_to(fake, "GET", "/v1/node/me") and len(requests_to(fake, "POST", "/v1/node/calls")) == 1


def test_one_shot_voice_notes(fake: FakeCloud, fixtures_dir):
    seed_voice_notes(fake)
    fake.push_targets[f"{ACCOUNT}:{WATCH}"] = PUSH_KEY
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        with c.websocket_connect("/v1/call", headers=watch(fake)) as ws:
            ws.send_json(START)  # no agent named: the first one
            ready = ws.receive_json()
            assert ready["type"] == "session.ready", ready
            assert ready["agent"]["slug"] == "voice-notes" and ready["turn_end"] == "manual"
            call_id = ready["call_id"]
            ws.send_bytes(speech(fixtures_dir)[: 16000 * 2 * 2])
            ws.send_json({"type": "session.end"})
        wait_for(lambda: fake.calls[call_id]["status"] == "delivered")
        stored = fake.calls[call_id]
        assert (stored["delivery"], stored["error"], stored["open"]) == ("history", None, False)
        assert stored["finished_at"] is not None
        # D21: one finish, `processing` at hang-up; the outcome came by PATCH.
        finishes = requests_to(fake, "POST", f"/{call_id}/finish")
        assert len(finishes) == 1
        wait_for(lambda: sealed_texts(fake, call_id) == [("user", "buy oat milk")])
        wait_for(lambda: len(fake.pushes) == 1)
        push = fake.pushes[0]
        assert (push["account"], push["device"], push["call"]) == (ACCOUNT, WATCH, call_id)
        assert push["message"]["event"] == "call.finished"
        assert push["message"]["data"]["status"] == "delivered"
        node = c.app.state.cloud_node
        wait_for(lambda: node.registry.ids() == [])
        assert node.registry.grant(call_id) is None
        assert c.app.state.call_slots.active() == {"conversation": 0, "one_way": 0}


def _refusal_setup(fake: FakeCloud, case: str):
    """State for each refusal; returns what the client must see: an `error` triple or a close code."""
    seed_voice_notes(fake)
    if case == "no_plan":
        fake.plans.pop(ACCOUNT)
        return PLAN_REQUIRED
    if case == "custom_operator_voice":
        fake.plans[ACCOUNT] = CUSTOM_PLAN
        return NEEDS_VOICE_URL
    if case == "quota_used":
        fake.used_s[ACCOUNT] = fake.quota_s
        return QUOTA_EXCEEDED
    if case == "too_many_calls":
        fake.seed_call(ACCOUNT, open=True, call_type="conversation")
        return TOO_MANY_CALLS
    if case == "cloud_down":
        return CLOUD_UNREACHABLE
    if case == "revoked":
        fake.revoked.add(WATCH)
        return 4401
    if case == "unknown_agent":
        return ("unknown_profile", None, "unknown agent: nope")
    return BUSY  # slots_full


@pytest.mark.parametrize("case", [
    "no_plan", "custom_operator_voice", "quota_used", "slots_full", "too_many_calls", "cloud_down", "revoked",
    "unknown_agent",
])
def test_refusals(fake: FakeCloud, case: str):
    expected = _refusal_setup(fake, case)
    before = set(fake.calls)
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        slots = c.app.state.call_slots
        held = [slots.try_acquire("one_way") for _ in range(10)] if case == "slots_full" else []
        headers = watch(fake)
        if case == "cloud_down":
            assert c.get("/v1/calls", headers=ios(fake)).status_code == 200  # the Cloud's keys are fetched
            fake.down = True
        start = {**START, "agent": "nope"} if case == "unknown_agent" else START
        with c.websocket_connect("/v1/call", headers=headers) as ws:
            ws.send_json(start)
            if isinstance(expected, int):
                assert closed_with(ws) == expected
            else:
                msg = ws.receive_json()
                assert (msg["type"], msg["fatal"]) == ("error", True)
                assert (msg["code"], msg.get("reason"), msg["message"]) == expected
                assert closed_with(ws) == 4400
        for slot in held:
            slot.release()
        assert slots.active() == {"conversation": 0, "one_way": 0}
    assert {k for k in fake.calls if k not in before and fake.calls[k]["open"]} == set()


def test_node_without_heartbeat_is_busy(fake: FakeCloud):
    seed_voice_notes(fake)
    fake.retry_after = 30  # every node route 429: no heartbeat accepted; the keys (not a node route) still load
    with TestClient(call_app(fake)) as c, c.websocket_connect("/v1/call", headers=watch(fake)) as ws:
        ws.send_json(START)
        msg = ws.receive_json()
        assert (msg["code"], msg.get("reason"), msg["message"], msg["fatal"]) == (*BUSY, True)
    assert fake.calls == {}


def test_draining_node_is_busy(fake: FakeCloud):
    seed_voice_notes(fake)
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        c.app.state.cloud_node.draining = True
        with c.websocket_connect("/v1/call", headers=watch(fake)) as ws:
            ws.send_json(START)
            msg = ws.receive_json()
            assert (msg["code"], msg.get("reason"), msg["message"]) == BUSY
    assert fake.calls == {}


@pytest.mark.parametrize("claims", [{"did": None}, {"plt": None}, {"aud": "https://other.test"}])
def test_bad_token_closes_4401(fake: FakeCloud, claims):
    seed_voice_notes(fake)
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        with c.websocket_connect("/v1/call", headers=watch(fake, **claims)) as ws:
            assert closed_with(ws) == 4401  # before session.start
    assert fake.calls == {}


def test_any_platform_may_call(fake: FakeCloud, fixtures_dir):
    # D11: the iPhone calls too.
    seed_voice_notes(fake)
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        with c.websocket_connect("/v1/call", headers=ios(fake)) as ws:
            ws.send_json(START)
            ready = ws.receive_json()
            assert ready["type"] == "session.ready"
            ws.send_json({"type": "session.end"})
        call_id = ready["call_id"]
        assert fake.calls[call_id]["platform"] == "ios"
        wait_for(lambda: not fake.calls[call_id]["open"])


def test_cloud_down_mid_call(fake: FakeCloud, llm):
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        create_talk(c, fake)
        with c.websocket_connect("/v1/call", headers=watch(fake)) as ws:
            ws.send_json({**START, "agent": "talk"})
            ready = ws.receive_json()
            call_id = ready["call_id"]
            fake.down = True
            ws.send_bytes(tone(500) + silence(900))
            events = read_until_agent_end(ws)  # the conversation goes on
            assert events[:3] == ["turn.user_end", "transcript", "turn.agent_start"] and "audio" in events
            ws.send_json({"type": "session.end"})
            assert closed_with(ws) == 1000
        node = c.app.state.cloud_node
        time.sleep(0.2)
        assert fake.calls[call_id]["open"] and call_id in node.registry.ids()  # held until the finish is taken
        back = len(fake.requests)
        fake.down = False
        wait_for(lambda: not fake.calls[call_id]["open"])
        wait_for(lambda: node.registry.ids() == [])
        assert sealed_texts(fake, call_id) == [("user", "hi"), ("agent", "Hello there.")]
        assert fake.calls[call_id]["status"] == "ended"
        entries = requests_to(fake, "POST", f"/{call_id}/entries", back)
        finishes = requests_to(fake, "POST", f"/{call_id}/finish", back)
        assert entries and finishes and max(entries) < min(finishes)  # in order


def test_quota_cut_mid_call(fake: FakeCloud, llm):
    fake.quota_s = 1  # remaining 1 s, so max_s 1 too: the minutes run out first
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        create_talk(c, fake)
        with c.websocket_connect("/v1/call", headers=watch(fake)) as ws:
            ws.send_json({**START, "agent": "talk"})
            ready = ws.receive_json()
            started = time.monotonic()
            msg = ws.receive_json()
            assert msg == {"type": "error", "code": QUOTA_EXCEEDED[0], "message": QUOTA_EXCEEDED[2], "fatal": False}
            assert 0.8 <= time.monotonic() - started < 3.0
            assert closed_with(ws) == 1000
        call_id = ready["call_id"]
        wait_for(lambda: not fake.calls[call_id]["open"])
        assert fake.calls[call_id]["status"] == "empty" and 1 <= fake.calls[call_id]["seconds"] <= 2


def test_max_conversation_cut(fake: FakeCloud, llm):
    fake.max_conversation_s = 1  # the call's limit comes first; plenty of minutes left
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        create_talk(c, fake)
        with c.websocket_connect("/v1/call", headers=watch(fake)) as ws:
            ws.send_json({**START, "agent": "talk"})
            ready = ws.receive_json()
            call_id = ready["call_id"]
            assert ws.receive_json() == {"type": "call.captured", "call_id": call_id, "reason": "limit"}
            assert closed_with(ws) == 1000
        wait_for(lambda: not fake.calls[call_id]["open"])
        assert 1 <= fake.calls[call_id]["seconds"] <= 2


def test_call_deadline():
    # D12: deadline = min(max_s, the local limit); the minutes run out first when quota_s <= deadline.
    def opened(quota_s: int, max_s: int) -> OpenedCall:
        return OpenedCall("c_1", "g", agent_record(), "full", quota_s, max_s, 0.0)

    assert call_deadline(opened(600, 600), 3600) == (600, True)
    assert call_deadline(opened(36000, 3600), 3600) == (3600, False)
    assert call_deadline(opened(36000, 3600), 60) == (60, False)  # the operator's cap is shorter
    assert call_deadline(opened(30, 30), 60) == (30, True)
    assert call_deadline(opened(36000, 1800), None) == (1800, False)


@pytest.mark.parametrize("quota", [True, False])
def test_one_way_wall_clock_cut(fake: FakeCloud, quota: bool):
    seed_voice_notes(fake)
    if quota:
        fake.quota_s = 1
    else:
        fake.max_one_way_s = 1
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        with c.websocket_connect("/v1/call", headers=watch(fake)) as ws:
            ws.send_json(START)
            ready = ws.receive_json()
            call_id = ready["call_id"]
            ws.send_bytes(silence(100))  # muted audio does not count toward max_call_ms; the wall clock does
            msg = ws.receive_json()
            if quota:
                assert (msg["type"], msg["code"], msg["fatal"]) == ("error", "quota_exceeded", False)
            else:
                assert msg == {"type": "call.captured", "call_id": call_id, "reason": "limit"}
            assert closed_with(ws) == 1000
        wait_for(lambda: fake.calls[call_id]["status"] in ("empty", "delivered", "failed"))
        assert fake.calls[call_id]["seconds"] <= 2


def test_client_gone_before_ready_finishes_empty(fake: FakeCloud, llm, monkeypatch):
    # The call opened on the Cloud, then session.ready could not be sent: finish `empty` with 0 s.
    from wristcall import protocol

    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        create_talk(c, fake)

        def boom(*_args, **_kwargs):
            raise RuntimeError("client gone")

        monkeypatch.setattr(protocol, "session_ready", boom)
        with pytest.raises(RuntimeError, match="client gone"):  # the TestClient raises the server's error
            with c.websocket_connect("/v1/call", headers=watch(fake)) as ws:
                ws.send_json({**START, "agent": "talk"})
                ws.receive_text()
        (call,) = opened_calls(fake)
        wait_for(lambda: not call["open"])
        assert (call["status"], call["seconds"]) == ("empty", 0)
        assert c.app.state.call_slots.active() == {"conversation": 0, "one_way": 0}


def test_logs_hold_no_token_or_grant(fake: FakeCloud, llm, caplog):
    caplog.set_level("DEBUG")
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        create_talk(c, fake)
        headers = watch(fake)
        with c.websocket_connect("/v1/call", headers=headers) as ws:
            ws.send_json({**START, "agent": "talk"})
            ready = ws.receive_json()
            ws.send_json({"type": "session.end"})
        wait_for(lambda: not fake.calls[ready["call_id"]]["open"])
    token = headers["Authorization"].split()[1]
    grants = list(fake.grants)
    assert grants and token not in caplog.text
    assert not any(g in caplog.text for g in grants)
    assert "sk-agent-secret" not in caplog.text


def test_self_hosted_call_unchanged():
    with TestClient(create_app(fake_config(), storage=open_sqlite_storage(":memory:"))) as c:
        assert c.app.state.cloud_node is None
        user = asyncio.run(c.app.state.storage.users.by_handle("owner"))
        code = asyncio.run(c.app.state.pairing.create_code(user.id)).code
        token = c.post("/v1/pair", json={"code": code, "device_name": "Watch"}).json()["token"]
        with c.websocket_connect("/v1/call", headers={"Authorization": f"Bearer {token}"}) as ws:
            ws.send_json(START)
            ready = ws.receive_json()
            assert ready["type"] == "session.ready" and ready["call_id"].startswith("c_")
            ws.send_bytes(tone(500) + silence(900))
            events = read_until_agent_end(ws)
            assert events[:3] == ["turn.user_end", "transcript", "turn.agent_start"]
            ws.send_json({"type": "session.end"})
            assert closed_with(ws) == 1000


# --- open_call and check_plan ------------------------------------------------------------------------------------


def agent_record(spec: dict | None = None, call_type: str = "one-shot") -> AgentRecord:
    return AgentRecord(id=VOICE_NOTES_ID, user_id=ACCOUNT, slug="voice-notes", display_name="Voice notes", icon="mic",
                       call_type=call_type, position=0, spec=spec or VOICE_NOTES_SPEC, created_at=0.0, updated_at=0.0)


def test_check_plan():
    check_plan("full", agent_record())
    own = {"type": "openai_stt", "base_url": "https://stt.example.test/v1", "model": "w"}
    check_plan("custom", agent_record({**VOICE_NOTES_SPEC, "stt": own}))
    with pytest.raises(CallRefused) as e:
        check_plan(None, agent_record())
    assert (e.value.code, e.value.reason, e.value.message) == PLAN_REQUIRED
    for spec in ({**VOICE_NOTES_SPEC}, {**VOICE_NOTES_SPEC, "stt": own, "tts": {"provider": "tts"}}):
        with pytest.raises(CallRefused) as e:
            check_plan("custom", agent_record(spec))
        assert (e.value.code, e.value.reason, e.value.message) == NEEDS_VOICE_URL


def build_node(fake: FakeCloud) -> CloudNode:
    config = cloud_config(providers=PROVIDERS)
    return CloudNode.build(config, slots=CallSlots(config.limits.max_concurrent_calls), http=fake.http())


def proof_for(fake: FakeCloud, device: str = WATCH) -> UserProof:
    return UserProof(ACCOUNT, device, "watchos", fake.user_token(ACCOUNT, device, "watchos"))


async def test_open_call_and_replay(fake: FakeCloud):
    seed_voice_notes(fake)
    node = build_node(fake)
    proof = proof_for(fake)
    opened = await open_call(node, proof, agent_record(), delivery="history", request_id="req-12345678")
    assert isinstance(opened, OpenedCall)
    assert (opened.plan, opened.quota_s, opened.max_s) == ("full", 600 * 60, 1800)
    assert opened.grant in fake.grants and opened.agent.id == VOICE_NOTES_ID
    assert opened.grant not in repr(opened)
    assert fake.calls[opened.call_id]["delivery"] == "history"
    again = await open_call(node, proof, agent_record(), delivery="history", request_id="req-12345678")  # 200 replay
    assert again.call_id == opened.call_id


async def test_open_call_asks_once_more_with_the_same_request_id(fake: FakeCloud):
    seed_voice_notes(fake)
    node = build_node(fake)
    real = node.client.request
    sent: list[dict] = []

    async def flaky(method, path, **kwargs):
        sent.append(kwargs["json"])
        resp = await real(method, path, **kwargs)
        if len(sent) == 1:  # the call opened, but the answer was lost
            raise CloudUnavailable("lost")
        return resp

    node.client.request = flaky
    opened = await open_call(node, proof_for(fake), agent_record(), delivery="history", request_id="req-abcdefgh")
    assert len(sent) == 2 and sent[0] == sent[1] and sent[0]["request_id"] == "req-abcdefgh"
    assert [c["id"] for c in fake.calls.values()] == [opened.call_id]


@pytest.mark.parametrize("setup, expected", [
    (lambda f: f.plans.pop(ACCOUNT), ("plan_required", None)),
    (lambda f: f.used_s.update({ACCOUNT: f.quota_s}), ("quota_exceeded", None)),
    (lambda f: f.revoked.add(WATCH), ("unauthorized", None)),
    (lambda f: f.seed_call(ACCOUNT, open=True), ("busy", "too_many_calls")),
    (lambda f: f.agents.clear(), ("unknown_profile", None)),
    (lambda f: setattr(f, "down", True), ("agent_unavailable", "cloud_unreachable")),
])
async def test_open_call_refusals(fake: FakeCloud, setup, expected):
    seed_voice_notes(fake)
    node = build_node(fake)
    setup(fake)
    with pytest.raises(CallRefused) as e:
        await open_call(node, proof_for(fake), agent_record(), delivery="history", request_id="req-12345678")
    assert (e.value.code, e.value.reason) == expected


async def test_open_call_odd_answer_is_cloud_unreachable(fake: FakeCloud):
    seed_voice_notes(fake)
    node = build_node(fake)
    real = node.client.request
    calls = 0

    async def odd(method, path, **kwargs):
        nonlocal calls
        calls += 1
        resp = await real(method, path, **kwargs)
        return type(resp)(status=resp.status, body={**resp.body, "max_s": "soon"}, headers=resp.headers)

    node.client.request = odd
    with pytest.raises(CallRefused) as e:
        await open_call(node, proof_for(fake), agent_record(), delivery="history", request_id="req-12345678")
    assert (e.value.code, e.value.reason) == ("agent_unavailable", "cloud_unreachable") and calls == 2


# --- fixes left by the reviews ---------------------------------------------------------------------------------------


async def test_identify_with_the_cloud_keys_unavailable_raises(fake: FakeCloud):
    verifier = OidcVerifier("https://cloud.test", ["https://node.test"], fake.http(), typ="wc-server+jwt")
    auth = CloudAuthenticator(verifier)
    header = f"Bearer {fake.user_token(ACCOUNT, WATCH, 'watchos')}"
    fake.down = True
    with pytest.raises(CloudUnavailable):
        await auth.identify(header)
    with pytest.raises(CloudUnavailable):
        await auth.authenticate(header)
    assert await auth.identify("Bearer not.a.jwt") is None  # a bad token stays a refusal


def test_rest_with_the_cloud_down_before_the_keys_is_503(fake: FakeCloud):
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        fake.down = True  # no request has fetched the Cloud's keys yet
        r = c.get("/v1/calls", headers=ios(fake))
        assert (r.status_code, r.json()["error"]) == (503, "cloud_unavailable")
        r = c.get("/v1/me", headers=ios(fake))
        assert (r.status_code, r.json()["error"]) == (503, "cloud_unavailable")


def test_websocket_with_the_cloud_down_before_the_keys_is_cloud_unreachable(fake: FakeCloud):
    seed_voice_notes(fake)
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        fake.down = True  # the node is ready, but the token cannot be checked: told after session.start
        with c.websocket_connect("/v1/call", headers=watch(fake)) as ws:
            ws.send_json(START)
            msg = ws.receive_json()
            assert (msg["code"], msg.get("reason"), msg["message"], msg["fatal"]) == (*CLOUD_UNREACHABLE, True)
            assert closed_with(ws) == 4400
    assert opened_calls(fake) == []


async def test_drain_with_no_calls_still_tells_the_cloud(fake: FakeCloud):
    config = cloud_config()
    slots = CallSlots(config.limits.max_concurrent_calls)
    node = CloudNode.build(config, slots=slots, http=fake.http())
    real = node.client.request

    async def slow(method, path, **kwargs):
        await asyncio.sleep(0.1)  # the beat takes a moment: a drain with no calls must not cancel it
        return await real(method, path, **kwargs)

    node.client.request = slow
    assert await node.drain_calls(slots, 5.0) == 0
    assert fake.heartbeats and fake.heartbeats[-1]["capacity"] == {"conversation": 0, "one_way": 0}


async def test_drain_gives_the_beat_up_after_tell_s(fake: FakeCloud):
    config = cloud_config()
    slots = CallSlots(config.limits.max_concurrent_calls)
    node = CloudNode.build(config, slots=slots, http=fake.http())

    async def hangs(method, path, **kwargs):
        await asyncio.sleep(30)

    node.client.request = hangs
    started = time.monotonic()
    assert await node.drain_calls(slots, 5.0, tell_s=0.1) == 0
    assert time.monotonic() - started < 1.0


def test_redeliver_while_draining_is_busy(fake: FakeCloud):
    with TestClient(call_app(fake)) as c:
        wait_ready(c)
        call_id = fake.seed_call(ACCOUNT, status="failed", error="delivery_failed", delivery="webhook")
        c.app.state.cloud_node.draining = True
        r = c.post(f"/v1/calls/{call_id}/redeliver", headers=ios(fake))
        assert (r.status_code, r.json()) == (409, {"error": "busy", "message": BUSY[2]})
        assert requests_to(fake, "POST", "/redeliver") == []


async def test_closing_stream_closes_the_views_when_the_body_fails_to_close():
    closed = []

    async def content():
        try:
            yield "a"
            yield "b"
        finally:
            raise RuntimeError("closing the body failed")

    async def views():
        try:
            yield {}
        finally:
            closed.append(True)

    body, rows = content(), views()
    await rows.__anext__()
    stream = _ClosingStream(body, rows, media_type="text/plain")

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("client went away")

    async def receive():
        await asyncio.sleep(10)
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "method": "GET", "headers": []}
    with pytest.raises(Exception):  # noqa: B017 - OSError or the close failure, whichever surfaces
        await stream(scope, receive, send)
    assert closed == [True]
