# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Each component follows [SemVer](https://semver.org/) on its own: the server is published by the `server-vX.Y.Z` tag (Docker image); the watch app is marked by the `watch-vX.Y.Z` tag and has no published binary (build it with Xcode, see [watch/README.md](watch/README.md)).

## [watch-0.2.0] - 2026-10-09

Needs server 0.3.0 or later for agents and turn modes and 0.4.0 or later for one-way agents. With 0.2.x servers it works as one conversation agent per profile.

### Watch app

- Several servers: pair more than one (Settings > Servers > Add server), remove them one by one. A watch paired with 0.1.0 keeps its server (the Keychain item is migrated). A server that is down does not stop the others, and a revoked one is removed on its own.
- Agent grid: Home shows the agents of every server with their icons; tap to call, long press for the call options.
- One-way agents (one-shot, monologue): "Recording" screen with "Send", then a progress ring, a check or the failure reason, and the transcribed text. The watch polls `GET /v1/calls/{id}` while the app is open, for up to 3 minutes ("Check again" after that); push comes later.
- "Call <agent>" complication, control and shortcut for a chosen agent; the 0.1.0 ones still call the first agent. They read the agent list from an App Group, which Xcode must register on the first build for your watch (see [watch/README.md](watch/README.md)).
- Debug launch arguments `-autoCallAgent`, `-showOptions`, `-addServer` and `-openURL` for the simulator.
- `WristcallKit`: agents and call types, `call_id` and `call.captured`, the call status client and poller, and the list of servers.

## [0.5.0] - 2026-10-09

### Added

- Call history: every call is recorded as text, delivered or not, conversations included (what the user said and
  the agent's answers; one-shot and monologue keep the transcript and the delivery status). Never audio.
- `GET /v1/calls` lists and searches a user's calls (`agent`, `q`, `since`, `until`, `before`, `limit`); search finds
  whole words ignoring case and accents, with SQLite FTS5.
- `DELETE /v1/calls/{id}`, `DELETE /v1/calls?agent=<ref>|all=true`, `GET /v1/calls/export?format=md|json` and
  `POST /v1/calls/{id}/redeliver` (a failed one-way delivery, again to the agent's webhook with the same
  `Idempotency-Key`). User API token only.
- `GET /v1/calls/{id}` adds `agent`, `expires_at` and `entries`; a conversation's `session.ready` adds `call_id`.
- Agents have `retention_days` (days, `"forever"` or `null` for the operator's default) and show
  `effective_retention_days`; operator settings in the new `history` section (`default_retention_days`,
  `max_retention_days`, `purge_every_s`). Without it the operator sets no default and no ceiling: calls are kept
  until deleted, unless an agent sets its own `retention_days`.
- Optional encryption at rest (`history.encryption_key`): AES-256-GCM for the text, keyed hashes of the words for the
  search. CLI: `wristcall history new-key|encrypt|decrypt`.
- CLI: `wristcall history list|show|rm|clear|export|redeliver` and `wristcall agents add|edit --retention`.
- Central account (optional, `central_account` in `wristcall.yaml`: `issuer`, `clients`, `device_credential`).
  Without it the server behaves as 0.4.0 and the new routes answer `404 not_configured`.
- `POST /v1/account/link` links a user to a central account login, proved by an API token or by an
  8 digit pairing code (with a code, the response carries a new `api_token`); `DELETE /v1/account/link` removes the link.
- `POST /v1/pair/account` pairs a device with a central account login: right away
  (`device_credential: attestation`) or after the linked user approves (`approval`).
- Pairing requests API for the linked user: `GET /v1/pairing-requests` and
  `POST /v1/pairing-requests/{id}/approve|deny`.
- `GET /v1/health` adds `account` (`{"issuer","device_credential"}` or `null`).
- `POST /v1/pair/poll` can answer `403 {"error":"limit"}` when the device limit blocks collecting an approved device
  (retry after revoking one); a denied request answers `410`.
- CLI: `wristcall users unlink`, `wristcall devices deny`, and a `linked` column in `wristcall users list`.
- Reference client: `wristcall-refclient login` (RFC 8628 device authorization against the account issuer) and
  `pair-account` (pairs through `POST /v1/pair/account`).

### Changed

- `wristcall devices approve` ignores requests aimed at another user.
- New dependency: `PyJWT[crypto]>=2.10`.
- A conversation record goes `recording` → `ended` (or `empty`); a server restart closes it with `error: "interrupted"`.

### Security

- A pairing code now also links a central account login and returns a management API token to whoever links first. Do
  not show codes in public places.
- Central access tokens are checked against the issuer's keys only: a login revoked at the issuer is accepted until its
  access token expires (use short access tokens). `users unlink` does not revoke devices already paired. In `attestation`
  mode a leaked token becomes a device until revoked.
- Every server accepts the same app client ids, so a central token handed to one server's operator can be replayed at
  another server's link and pair routes. Per server registration is planned (epic E6).
- The same token can also be replayed at the Cloud API (`cloud/`), which accepts the same watch, iOS and PWA client ids:
  a malicious self-hosted operator could read the victim's agenda and add or delete servers (for example a phishing
  "Home" entry). Only `DELETE /v1/account` is restricted (iOS and PWA clients). Per-server token audiences (epic E6)
  must ship before the Cloud API is deployed.
- With an encryption key set, the server records which key it is and refuses to start with another key or without
  one: losing the key loses the encrypted history. The search index of encrypted text reveals which entries share a
  word (not the word).
- History routes take the user's API token only: a device token can read just one call (`GET /v1/calls/{id}`).

### Upgrade note

- The database gains schema steps 4 (central account) and 5 (history: the text of 0.4.0's one-way calls moves to
  `call_entries`); rolling back to 0.4.0 needs the backup taken before the upgrade (running step 4 again on a
  migrated database fails with a duplicate column error). Export the history first to keep calls made since.

## [0.4.0] - 2026-10-09

### Protocol

- Agents can be `one-shot` (say one thing and hang up) or `monologue` (talk until you
  hang up): the server only records, then transcribes and posts the text to the agent's
  webhook. Silence never ends these calls and mute only pauses the recording.
- `session.ready` of a one-way call adds `call_id`; new server message `call.captured`
  when the server stops recording at the time limit. Watch 0.1.0 and clients of server 0.3.x keep
  working (they ignore both).
- New `GET /v1/calls/{call_id}` (device or API token): `recording`, `processing`, then
  `delivered`, `failed` (`stt_failed`, `delivery_failed`, `interrupted`) or `empty`,
  with the text and the delivery attempts.
- `GET /v1/providers` may list providers of kind `webhook`.

### Server (`server/`)

- Delivery: `POST` of a JSON `call.completed` body with the agent's headers and the call id
  as `Idempotency-Key`; any `2xx` is delivered; three attempts (15 s each, retries after
  3 s and 6 s) within about a minute; redirects are not followed. The text is kept in the
  new `calls` table (database version 3) whether or not it was delivered.
- New provider type `webhook` (`url`, `headers`), allowed in custom endpoints by default
  (`limits.custom_endpoint_types`). New `limits.max_one_way_call_s` (1800).
- A one-way agent's `action` is a webhook and its `tts` is optional; `call_type` can change
  together with an `action` of the matching kind. Calls left processing by a stopped server
  are marked `interrupted` on the next start.

### Reference client (`tools/refclient`)

- `call --agent <slug>`; with `--wav`, a one-way agent hangs up after the file and waits
  for the delivery result.

## [0.3.0] - 2026-10-09

### Protocol

- `session.start` accepts an optional `agent` (slug or id); `profile` keeps working as
  a slug. Without `turn_end`, a call that names its `agent` uses the agent's own mode;
  0.2.x clients (only `profile`) keep `auto`.
- `session.ready` adds `agent` and `turn_end`; `GET /v1/me` adds `user` and `agents`.
  The 0.2.x fields (`profile`, `profiles`) stay, so watch 0.1.0 keeps working.
- New fatal error `agent_unavailable`. New management API (`/v1/agents`,
  `/v1/providers`, `/v1/devices`, `/v1/pairing-codes`) with per-user API tokens.

### Server (`server/`)

- Multi-user: users, their watches and their agents live in the database, behind a
  storage interface (SQLite adapter) with versioned migrations (`PRAGMA user_version`).
  The database of 0.2.0 is migrated on start; 0.2.0 still runs on a migrated database.
- Agents: name, SF Symbol icon, STT, action, TTS (a provider of the server or the user's
  own URL), language, prompt, turn end (`auto`/`manual`) and silence. CLI:
  `wristcall users ...`, `wristcall users tokens ...`, `wristcall agents ...`;
  `pair` and `devices approve` take `--user`.
- `profiles` in `wristcall.yaml` are imported once as agents of a user `owner`
  (`default` first) and existing watches are given to that user; the section is
  ignored afterwards. New `limits` section (agents and devices per user, custom
  endpoints on/off).
- Every provider in the YAML is checked at startup. FTS5 is required from SQLite.
- Secrets of custom endpoints are redacted at any depth, `base_url` cannot carry
  credentials, and `vad`/`timeouts` values are bounded. `wristcall devices assign`
  gives a watch to a user.
- Agent `vad`/`timeouts` values are bounded (for example `silence_ms` 100 to 10000,
  timeouts up to 120 s); imported 0.2.0 profiles keep loading, and values outside
  the bounds are adjusted on import with a warning in the log.
- Custom endpoints accept only the types in `limits.custom_endpoint_types` (by default
  `openai_stt`, `openai_chat` and `openai_tts`), and TTS `sample_rate` must be between
  8000 and 48000.

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
