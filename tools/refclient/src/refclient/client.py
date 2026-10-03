"""Client core: pairing (HTTP) and calls (WebSocket), with no dependency on system audio."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

FRAME_BYTES = 640
CLOSE_UNAUTHORIZED = 4401
START = {"type": "session.start", "protocol": 1, "audio_in": {"codec": "pcm16", "sample_rate": 16000, "channels": 1}}


class PairError(Exception):
    pass


class CallError(Exception):
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
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(poll_interval_s)
        # poll_token is a secret: it goes in the body, never in the path (which shows up in access logs).
        p = client.post(f"{base}/v1/pair/poll", json={"poll_token": body["poll_token"]})
        if p.status_code == 200:
            return p.json()
        if p.status_code != 202:
            raise PairError(f"request closed ({p.status_code}): {p.text[:200]}")
    raise PairError("approval timed out")


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


async def run_call(
    server: str,
    token: str,
    source: AsyncIterator[bytes | dict[str, Any]],
    *,
    profile: str | None = None,
    stop_after_agent_turns: int | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    on_audio: Callable[[bytes], None] | None = None,
    stop_on_error: bool = False,
) -> CallResult:
    """Makes a call. With stop_on_error=True, a non fatal `error` also ends it (e.g. stt_failed in --wav mode)."""
    result = CallResult()
    start = dict(START, profile=profile) if profile else START
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
