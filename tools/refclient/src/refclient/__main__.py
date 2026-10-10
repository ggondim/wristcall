"""wristcall-refclient: pair with and call a wristcall server from the Mac."""

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from pathlib import Path

import httpx

from . import device_flow
from .client import (
    CallError,
    PairError,
    RefclientError,
    pair,
    pair_account,
    require_cloud,
    run_call,
    server_token,
    wait_for_call,
    wav_source,
)

CONFIG = Path(os.environ.get("WRISTCALL_REFCLIENT_CONFIG", Path.home() / ".config" / "wristcall" / "refclient.json"))


# Where `login` keeps the central account tokens (next to CONFIG).
ACCOUNT = CONFIG.parent / "account.json"


def _print_event(event: dict) -> None:
    kind = event["type"]
    if kind == "transcript":
        who = "you" if event["role"] == "user" else "agent"
        print(f"{who}: {event['text']}", flush=True)
    elif kind == "error":
        print(f"error {event['code']}: {event['message']}", file=sys.stderr, flush=True)
    elif kind == "session.ready":
        print(f"connected to profile {event['profile']['display_name']}. Enter toggles mute; Ctrl+C hangs up.", flush=True)
        if event.get("call_id"):
            print(f"one-way call {event['call_id']} ({event['agent']['call_type']}): nothing comes back", flush=True)
    elif kind == "call.captured":
        print("the server stopped recording (time limit)", flush=True)


def cmd_pair(args: argparse.Namespace) -> int:
    try:
        creds = pair(args.server, args.code, args.name)
    except PairError as e:
        print(e, file=sys.stderr)
        return 1
    _save_credentials(args.server, creds)
    return 0


def _save_credentials(server: str, creds: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"server": server, "token": creds["token"]}), encoding="utf-8")
    CONFIG.chmod(0o600)
    print(f"Paired as {creds['device_id']}. Credentials saved to {CONFIG}.")


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created 0o600 from the start: the file holds tokens.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    path.chmod(0o600)


def cmd_login(args: argparse.Namespace) -> int:
    try:
        with httpx.Client(timeout=10.0) as http:
            code, token_endpoint = device_flow.start(args.issuer, args.client_id, args.scope, http)
            print(f"Open {code.verification_uri_complete or code.verification_uri} and sign in.")
            if not code.verification_uri_complete:
                print(f"Code: {code.user_code}")
            print("Waiting for the login...", flush=True)
            tokens = device_flow.wait_for_token(token_endpoint, args.client_id, code, http)
    except device_flow.DeviceFlowError as e:
        print(e, file=sys.stderr)
        return 1
    account = {"issuer": args.issuer, "access_token": tokens["access_token"]}
    if tokens.get("refresh_token"):
        account["refresh_token"] = tokens["refresh_token"]
    if tokens.get("expires_in"):
        account["expires_at"] = int(time.time()) + int(tokens["expires_in"])
    _write_private(ACCOUNT, account)
    print(f"Logged in. Account tokens saved to {ACCOUNT}.")
    return 0


def cmd_pair_account(args: argparse.Namespace) -> int:
    try:
        account = json.loads(ACCOUNT.read_text(encoding="utf-8"))
        token = account["access_token"]
    except (OSError, ValueError, KeyError, TypeError):
        print("no account login: run `wristcall-refclient login` first", file=sys.stderr)
        return 1
    if account.get("expires_at") and account["expires_at"] <= time.time():
        print("the account login expired: run `wristcall-refclient login` again", file=sys.stderr)
        return 1
    try:
        with httpx.Client(timeout=10.0) as http:
            # The Cloud comes from the user, never from the server; the token is asked for the URL as typed.
            require_cloud(args.server, args.cloud, http=http)
            server_tok = server_token(args.cloud, token, args.server, http=http)
            creds = pair_account(args.server, server_tok, args.name, http=http)
    except RefclientError as e:
        print(e, file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"request failed: {type(e).__name__}", file=sys.stderr)
        return 1
    _save_credentials(args.server, creds)
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    saved = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    server = args.server or saved.get("server")
    token = args.token or saved.get("token")
    if not server or not token:
        print("no credentials: run `wristcall-refclient pair` first", file=sys.stderr)
        return 1
    try:
        if args.wav:
            return asyncio.run(_call_wav(server, token, args))
        return asyncio.run(_call_mic(server, token, args))
    except CallError as e:
        print(e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


async def _call_wav(server: str, token: str, args: argparse.Namespace) -> int:
    with wave.open(args.wav) as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
            print("the WAV must be mono, 16 bit PCM, 16 kHz", file=sys.stderr)
            return 1
        pcm = w.readframes(w.getnframes())
    try:
        result = await asyncio.wait_for(
            run_call(
                server, token, wav_source(pcm, realtime=not args.fast), profile=args.profile, agent=args.agent,
                stop_after_agent_turns=1, stop_on_error=True, on_event=_print_event,
            ),
            timeout=args.timeout,
        )
    except TimeoutError:
        print(
            f"no response from the agent within {args.timeout:g} s (empty transcript or stuck server?); giving up",
            file=sys.stderr,
        )
        return 1
    if result.call_id:
        view = await asyncio.to_thread(wait_for_call, server, token, result.call_id, timeout_s=args.timeout)
        print(f"{view['status']}" + (f" ({view['error']})" if view["error"] else "") + f": {view['text'] or ''}")
        return 0 if view["status"] == "delivered" else 1
    if args.out:
        with wave.open(args.out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(result.sample_rate)
            w.writeframes(bytes(result.audio))
        print(f"response saved to {args.out}")
    return 0


async def _call_mic(server: str, token: str, args: argparse.Namespace) -> int:
    from .audio_io import Speaker, mic_source

    speaker: Speaker | None = None

    def on_event(event: dict) -> None:
        nonlocal speaker
        if event["type"] == "session.ready":
            speaker = Speaker(event["audio_out"]["sample_rate"])
        _print_event(event)

    def on_audio(data: bytes) -> None:
        if speaker:
            speaker.play(data)

    try:
        await run_call(server, token, mic_source(), profile=args.profile, agent=args.agent, on_event=on_event, on_audio=on_audio)
    finally:
        if speaker:
            speaker.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="wristcall-refclient")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pair", help="pair with a server")
    p.add_argument("--server", required=True, help="server URL, e.g. https://wristcall.yourdomain.com")
    p.add_argument("--code", help="8 digit code (omit in manual approval mode)")
    p.add_argument("--name", default="refclient", help="name of this device")
    p.set_defaults(func=cmd_pair)
    lg = sub.add_parser("login", help="sign in to a central account (device authorization, RFC 8628)")
    lg.add_argument("--issuer", required=True, help="issuer URL of the central account, e.g. https://auth.trigram.com.br")
    lg.add_argument("--client-id", required=True, help="public client id registered at the issuer (device_code grant)")
    lg.add_argument("--scope", default="openid profile offline_access")
    lg.set_defaults(func=cmd_login)
    pa = sub.add_parser("pair-account", help="pair with a server using the central account login")
    pa.add_argument("--server", required=True, help="server URL, e.g. https://wristcall.yourdomain.com")
    pa.add_argument("--cloud", required=True, help="wristcall Cloud URL the server trusts (its central_account.issuer)")
    pa.add_argument("--name", default="refclient", help="name of this device")
    pa.set_defaults(func=cmd_pair_account)
    c = sub.add_parser("call", help="call the agent")
    c.add_argument("--server")
    c.add_argument("--token")
    c.add_argument("--profile")
    c.add_argument("--agent", help="agent slug or id (server 0.3.0+); wins over --profile")
    c.add_argument("--wav", help=(
        "instead of the microphone, send this WAV (16 kHz mono) and exit after the response or the first error; "
        "a one-shot or monologue agent then hangs up and waits for the delivery"
    ))
    c.add_argument("--out", help="with --wav: save the response audio to this WAV")
    c.add_argument("--fast", action="store_true", help="with --wav: send without waiting for real time")
    c.add_argument(
        "--timeout", type=float, default=120.0,
        help="with --wav: give up if the response does not finish within N seconds (default 120)",
    )
    c.set_defaults(func=cmd_call)
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
