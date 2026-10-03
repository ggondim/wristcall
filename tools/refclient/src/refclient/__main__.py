"""wristcall-refclient: pair with and call a wristcall server from the Mac."""

import argparse
import asyncio
import json
import os
import sys
import wave
from pathlib import Path

from .client import CallError, PairError, pair, run_call, wav_source

CONFIG = Path(os.environ.get("WRISTCALL_REFCLIENT_CONFIG", Path.home() / ".config" / "wristcall" / "refclient.json"))


def _print_event(event: dict) -> None:
    kind = event["type"]
    if kind == "transcript":
        who = "you" if event["role"] == "user" else "agent"
        print(f"{who}: {event['text']}", flush=True)
    elif kind == "error":
        print(f"error {event['code']}: {event['message']}", file=sys.stderr, flush=True)
    elif kind == "session.ready":
        print(f"connected to profile {event['profile']['display_name']}. Enter toggles mute; Ctrl+C hangs up.", flush=True)


def cmd_pair(args: argparse.Namespace) -> int:
    try:
        creds = pair(args.server, args.code, args.name)
    except PairError as e:
        print(e, file=sys.stderr)
        return 1
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"server": args.server, "token": creds["token"]}), encoding="utf-8")
    CONFIG.chmod(0o600)
    print(f"Paired as {creds['device_id']}. Credentials saved to {CONFIG}.")
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
                server, token, wav_source(pcm, realtime=not args.fast), profile=args.profile,
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
        await run_call(server, token, mic_source(), profile=args.profile, on_event=on_event, on_audio=on_audio)
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
    c = sub.add_parser("call", help="call the agent")
    c.add_argument("--server")
    c.add_argument("--token")
    c.add_argument("--profile")
    c.add_argument("--wav", help="instead of the microphone, send this WAV (16 kHz mono) and exit after the response or the first error")
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
