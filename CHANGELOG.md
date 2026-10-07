# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Each component follows [SemVer](https://semver.org/) on its own: the server is published by the `server-vX.Y.Z` tag (Docker image); the watch app is marked by the `watch-vX.Y.Z` tag and has no published binary (build it with Xcode, see [watch/README.md](watch/README.md)).

## [watch-0.1.0] - 2026-10-07

First version of the watch app (`watch/`), validated on an Apple Watch Series 7 signed with a free Apple ID.

### Watch app

- Watch-only app for watchOS 26: pairs with a wristcall server by 8 digit code (through the pairing directory or a server URL) or by owner approval, and keeps the device token only in the Keychain.
- Calls through the native call screen (CallKit): microphone at any format converted to 16 kHz frames, the agent's voice played back at the server's rate, mute and hang up from the system screen.
- Per call end of turn: the Call button ends your turn when you pause; "…" opens call options with "Call (auto)" and "Call (manual)", where only the mute button ends your turn (needs server 0.2.0).
- Works over Wi-Fi and cellular without the iPhone. With no network at all the app shows "No connection" instead of starting a call.
- Starts a call from a complication, a Control Center control or the "Call agent" shortcut.
- `WristcallKit` Swift package with the protocol, pairing, audio and call session logic, tested on the Mac and in CI.

## [0.2.0] - 2026-10-07

### Protocol

- `session.start` accepts the optional field `turn_end` (`"auto"` or `"manual"`, absent = `"auto"`). Additive change within protocol 1; servers 0.1.x ignore the field and run every call as `"auto"`. See [docs/protocol.md](docs/protocol.md#end-of-the-users-turn).

### Server (`server/`)

- Added: per call end of turn mode. `"auto"` keeps the current behaviour (silence, mute or duration limit). `"manual"` never ends the turn by silence: only the mute or the duration limit end it, and every frame from the start of speech is kept, pauses included. A manual turn that reaches the limit with less than `min_speech_ms` of speech is dropped silently as noise.
- An unknown `turn_end` value is rejected at opening with `bad_message`.

## [0.1.0] - 2026-10-03

First public release.

### Server (`server/`)

- Protocol v1 between watch and server: pairing over REST and calls over WebSocket with PCM16 audio (16 kHz input, TTS rate on output). Contract in [docs/protocol.md](docs/protocol.md).
- End of turn by silence (Silero v6 VAD or energy based) or by the mute button on the call screen; per turn duration limit.
- Three stage pipeline, swappable through configuration: transcription, responder and voice, with adapters compatible with the OpenAI API and demo providers that need no key.
- Streaming response spoken sentence by sentence, wait for playback on the watch before listening again, and non fatal errors with an apology sentence.
- Named profiles that inherit from the `default` profile.
- Pairing by 8 digit code (valid for 10 minutes, single use) or by manual approval in the CLI; device tokens stored only as hashes in SQLite.
- `wristcall` CLI: `serve`, `pair` and `devices list|approve|revoke`.
- Optional warm-up of transcription and voice models that unload when idle.
- Docker image and `docker-compose.example.yml` with automatic TLS through Caddy.

### Reference client (`tools/refclient/`)

- `wristcall-refclient pair` and `call`, through the computer's microphone or a WAV file, speaking the same protocol as the watch.

### Pairing directory (`directory/`)

- Cloudflare Worker that exchanges the 8 digit code for the server URL. The project's public directory is at `https://wristcall-pair.trigram.com.br`; anyone can run their own.
