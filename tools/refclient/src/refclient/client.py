"""Client core: pairing (HTTP) and calls (WebSocket), with no dependency on system audio."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .device_flow import USER_AGENT

FRAME_BYTES = 640
CLOSE_UNAUTHORIZED = 4401
START = {"type": "session.start", "protocol": 1, "audio_in": {"codec": "pcm16", "sample_rate": 16000, "channels": 1}}
# Calls that only record: the server transcribes after hang-up and posts the text to the agent's webhook.
ONE_WAY = ("one-shot", "monologue")


class RefclientError(Exception):
    """A failure to show the user. Never carries a token."""


class PairError(RefclientError):
    pass


class CallError(RefclientError):
    pass


def call_url(server: str) -> str:
    base = server.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/v1/call"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/v1/call"
    raise ValueError("the server URL must start with http:// or https://")


def pair(
    server: str,
    code: str | None,
    device_name: str,
    *,
    poll_interval_s: float = 2.0,
    timeout_s: float = 600.0,
    http: httpx.Client | None = None,
) -> dict[str, str]:
    client = http or httpx.Client(timeout=10.0)
    base = server.rstrip("/")
    r = client.post(f"{base}/v1/pair", json={"code": code, "device_name": device_name})
    if r.status_code == 200:
        return r.json()
    if r.status_code != 202:
        raise PairError(f"pairing rejected ({r.status_code}): {r.text[:200]}")
    body = r.json()
    print(f"Request {body['request_id']} waiting for approval. On the server: wristcall devices approve {body['request_id']}")
    return _wait_approval(client, base, body["poll_token"], poll_interval_s, timeout_s)


def _wait_approval(client: httpx.Client, base: str, poll_token: str, poll_interval_s: float, timeout_s: float) -> dict[str, str]:
    """Polls /v1/pair/poll until an approver decides; shared by `pair` and `pair_account`."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(poll_interval_s)
        # poll_token is a secret: it goes in the body, never in the path (which shows up in access logs).
        p = client.post(f"{base}/v1/pair/poll", json={"poll_token": poll_token})
        if p.status_code == 200:
            return p.json()
        if p.status_code != 202:
            raise PairError(f"request closed ({p.status_code}): {p.text[:200]}")
    raise PairError("approval timed out")


def _error_name(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return "unknown"
    return str(body.get("error", "unknown")) if isinstance(body, dict) else "unknown"


def _same_url(a: str, b: str) -> bool:
    """Compared in normalized form: httpx lowercases scheme and host; default ports and trailing slash dropped.

    Only a yes/no on the server's claim: the token request always goes to the Cloud the user gave (`--cloud`),
    never to the issuer the server names."""
    try:
        x, y = httpx.URL(a), httpx.URL(b)
    except httpx.InvalidURL:
        return False

    def key(url: httpx.URL) -> tuple:
        return url.scheme, url.host, url.port or {"https": 443, "http": 80}.get(url.scheme), url.path.rstrip("/")

    return key(x) == key(y)


def _printable(value: object, limit: int = 200) -> str:
    """Text a server chose, safe to print: no terminal escapes or other control/format characters."""
    return "".join(c for c in str(value) if c.isprintable())[:limit]


@contextmanager
def _client(http: httpx.Client | None) -> Iterator[httpx.Client]:
    if http is not None:
        yield http
    else:
        with httpx.Client(timeout=10.0) as own:
            yield own


def require_cloud(server: str, cloud: str, *, http: httpx.Client | None = None) -> None:
    """Refuses a server whose central account is not `cloud` (the Cloud the user chose, never one the server names).

    A server announcing a Cloud of its own would get the login token, which is good at the real Cloud."""
    with _client(http) as client:
        r = client.get(f"{server.rstrip('/')}/v1/health", headers={"User-Agent": USER_AGENT})
    try:
        account = r.json().get("account") if r.status_code == 200 else None
    except (ValueError, AttributeError):
        account = None
    if r.status_code != 200:
        raise RefclientError(f"cannot read the server's health ({r.status_code})")
    if not isinstance(account, dict):
        raise RefclientError("this server has no central account")
    issuer = account.get("issuer")
    if not isinstance(issuer, str) or not _same_url(issuer, cloud):
        raise RefclientError(f"this server trusts another central account: {_printable(issuer)}")


def server_token(cloud: str, account_token: str, audience: str, *, http: httpx.Client | None = None) -> str:
    """A token the Cloud makes for one server only (`audience`, its URL as the user typed it) from the central
    account login. The login token itself never goes to a server."""
    with _client(http) as client:
        r = client.post(
            f"{cloud.rstrip('/')}/v1/server-tokens",
            json={"audience": audience},
            headers={"Authorization": f"Bearer {account_token}", "User-Agent": USER_AGENT},
        )
    if r.status_code != 200:
        raise RefclientError(f"the cloud refused a token for this server ({r.status_code}): {_error_name(r)}")
    try:
        token = r.json().get("token")
    except (ValueError, AttributeError):
        token = None
    if not isinstance(token, str) or not token:
        raise RefclientError("the cloud answered without a token")
    return token


def pair_account(
    server: str,
    account_token: str,
    device_name: str,
    *,
    poll_interval_s: float = 2.0,
    timeout_s: float = 600.0,
    http: httpx.Client | None = None,
) -> dict[str, str]:
    """Pairs with the user linked to a central account login (the token from `login`)."""
    client = http or httpx.Client(timeout=10.0)
    base = server.rstrip("/")
    r = client.post(f"{base}/v1/pair/account", json={"token": account_token, "device_name": device_name})
    if r.status_code == 200:
        return r.json()
    if r.status_code != 202:
        raise PairError(f"pairing rejected ({r.status_code}): {_error_name(r)}")
    body = r.json()
    print(
        f"Waiting for approval of request {body['request_id']} in the wristcall app "
        f"(or: wristcall devices approve {body['request_id']})"
    )
    return _wait_approval(client, base, body["poll_token"], poll_interval_s, timeout_s)


async def wav_source(pcm: bytes, *, realtime: bool = True, trailing_silence_ms: int = 1200) -> AsyncIterator[bytes]:
    """Sends the recorded audio in 20 ms frames, then silence so the VAD closes the turn."""
    if len(pcm) % FRAME_BYTES:
        pcm += b"\x00" * (FRAME_BYTES - len(pcm) % FRAME_BYTES)
    frames = [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
    frames += [b"\x00" * FRAME_BYTES] * (trailing_silence_ms // 20)
    for frame in frames:
        yield frame
        await asyncio.sleep(0.02 if realtime else 0)


@dataclass
class CallResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    audio: bytearray = field(default_factory=bytearray)
    sample_rate: int = 0
    # One-way calls: ask GET /v1/calls/{call_id} (wait_for_call) how the delivery went.
    call_id: str | None = None


async def run_call(
    server: str,
    token: str,
    source: AsyncIterator[bytes | dict[str, Any]],
    *,
    profile: str | None = None,
    agent: str | None = None,
    stop_after_agent_turns: int | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    on_audio: Callable[[bytes], None] | None = None,
    stop_on_error: bool = False,
) -> CallResult:
    """Makes a call. With stop_on_error=True, a non fatal `error` also ends it (e.g. stt_failed in --wav mode).

    A one-way agent (one-shot, monologue) gets the whole source and then the hang-up.
    """
    result = CallResult()
    start = dict(START)
    if profile:
        start["profile"] = profile
    if agent:
        start["agent"] = agent
    try:
        await _run(server, token, source, start, stop_after_agent_turns, stop_on_error, on_event, on_audio, result)
    except ConnectionClosed as e:
        if e.rcvd is not None and e.rcvd.code == CLOSE_UNAUTHORIZED:
            raise CallError("invalid or revoked token: run `wristcall-refclient pair` again") from e
        raise
    return result


async def _run(
    server: str,
    token: str,
    source: AsyncIterator[bytes | dict[str, Any]],
    start: dict[str, Any],
    stop_after_agent_turns: int | None,
    stop_on_error: bool,
    on_event: Callable[[dict[str, Any]], None] | None,
    on_audio: Callable[[bytes], None] | None,
    result: CallResult,
) -> None:
    async with connect(call_url(server), additional_headers={"Authorization": f"Bearer {token}"}, max_size=None) as ws:
        await ws.send(json.dumps(start))
        ready = json.loads(await ws.recv())
        if ready.get("type") != "session.ready":
            raise CallError(f"session start rejected: {ready}")
        result.sample_rate = ready["audio_out"]["sample_rate"]
        result.call_id = ready.get("call_id")
        one_way = (ready.get("agent") or {}).get("call_type") in ONE_WAY
        result.events.append(ready)
        if on_event:
            on_event(ready)

        async def send_loop() -> None:
            async for item in source:
                await ws.send(item if isinstance(item, bytes) else json.dumps(item))

        send_error: list[BaseException] = []
        closers: set[asyncio.Task] = set()

        def sender_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            err = task.exception()
            # Connection already closed: the receive loop ends on its own, this is not a send failure.
            if err is None or isinstance(err, ConnectionClosed):
                return
            send_error.append(err)
            closer = asyncio.ensure_future(ws.close())
            closers.add(closer)
            closer.add_done_callback(closers.discard)

        sender = asyncio.create_task(send_loop())
        sender.add_done_callback(sender_done)
        agent_turns = 0
        try:
            if one_way:
                await _record(ws, sender, result, on_event)
            else:
                async for message in ws:
                    if isinstance(message, bytes):
                        result.audio.extend(message)
                        if on_audio:
                            on_audio(message)
                        continue
                    event = json.loads(message)
                    result.events.append(event)
                    if on_event:
                        on_event(event)
                    if event["type"] == "error" and (event.get("fatal") or stop_on_error):
                        break
                    if event["type"] == "turn.agent_end":
                        agent_turns += 1
                        if stop_after_agent_turns and agent_turns >= stop_after_agent_turns:
                            break
        finally:
            sender.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await sender
            with suppress(Exception):
                await ws.send(json.dumps({"type": "session.end"}))
        if send_error:
            raise CallError(f"failed to send audio: {send_error[0]}") from send_error[0]


async def _record(ws: Any, sender: asyncio.Task, result: CallResult, on_event: Callable[[dict[str, Any]], None] | None) -> None:
    """One-way call: until the source ends (then the caller hangs up) or the server stops recording (call.captured)."""

    async def collect() -> None:
        async for message in ws:
            if isinstance(message, str):
                event = json.loads(message)
                result.events.append(event)
                if on_event:
                    on_event(event)

    receiver = asyncio.create_task(collect())
    try:
        await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        receiver.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await receiver


def call_status(server: str, token: str, call_id: str, *, http: httpx.Client | None = None) -> dict[str, Any]:
    url = f"{server.rstrip('/')}/v1/calls/{call_id}"
    headers = {"Authorization": f"Bearer {token}"}
    if http is not None:
        r = http.get(url, headers=headers)
    else:
        with httpx.Client(timeout=10.0) as own:
            r = own.get(url, headers=headers)
    if r.status_code != 200:
        raise CallError(f"call status failed ({r.status_code}): {r.text[:200]}")
    return r.json()


def wait_for_call(
    server: str, token: str, call_id: str, *, poll_interval_s: float = 1.0, timeout_s: float = 120.0
) -> dict[str, Any]:
    """Polls until the call is delivered, failed or empty (what the watch shows as ring, then check or error)."""
    deadline = time.monotonic() + timeout_s
    with httpx.Client(timeout=10.0) as http:
        while True:
            view = call_status(server, token, call_id, http=http)
            if view["status"] not in ("recording", "processing"):
                return view
            if time.monotonic() > deadline:
                raise CallError(f"call {call_id} still {view['status']} after {timeout_s:g} s")
            time.sleep(poll_interval_s)
