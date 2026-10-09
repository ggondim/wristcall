# wristcall protocol v1

Contract between a voice client (the Apple Watch app, the reference client
or any other) and a wristcall server. Version: **1**.

## Transport

- Server behind TLS: `https://` for REST and `wss://` for the call.
- Authentication: `Authorization: Bearer <token>` on all protected routes and on
  the WebSocket handshake. The token comes from pairing.
- The server sends a WebSocket ping every 20 s. Clients must answer with a pong
  (libraries do this on their own). This keeps the call alive behind proxies
  that drop idle connections.

## Pairing (REST, JSON)

| Route | Body | Responses |
|---|---|---|
| `GET /v1/health` | | `200 {"status":"ok","version":"0.3.0","protocol":1}` |
| `POST /v1/pair` | `{"code": "12345678" \| null, "device_name": "Apple Watch"}` | `200 {"device_id","token"}`: paired (flow A). `202 {"request_id","poll_token","expires_at"}`: waiting for the owner's approval (flow B). `401 {"error":"invalid_code"}`. `429 {"error":"rate_limited"}` |
| `POST /v1/pair/poll` | `{"poll_token": "..."}` | `202 {"request_id","expires_at"}`: pending. `200 {"device_id","token"}`: approved (delivered only once). `410 {"error":"gone"}`: expired or already delivered. `422`: body without `poll_token` or with more than 128 characters |
| `GET /v1/me` | | `200 {"device_id","device_name","user":{"id","handle","display_name"},"agents":[agent],"profiles":[{"name","display_name"}]}`. `401` |
| `DELETE /v1/me` | | `204`: token revoked. `401` |

Rules:
- The code has 8 digits; spaces and hyphens are ignored. It is valid for 10 minutes, once.
- In manual mode, `401` is also returned when there are too many pending requests (try again later). An invalid body returns `422`.
- In flow B, the client shows `request_id` (4 digits) so the owner can approve it with
  `wristcall devices approve <request_id>`, and polls `POST /v1/pair/poll` every
  2 s with the `poll_token` in the body. The `poll_token` is a client secret; never
  display it or put it in the URL (the path shows up in access logs).
- `expires_at` is epoch in seconds (float).
- A device belongs to the user who issued the code (`wristcall pair --user`, or
  `POST /v1/pairing-codes`) or who approved the request. `GET /v1/me` lists that
  user's agents in the watch's order: `agents` holds the agent summary
  (`{"id","slug","display_name","icon","call_type","turn_end"}`, see
  [Agents](#agents)); `profiles` repeats `slug` and `display_name` as `name` and
  `display_name`, the 0.2.x shape. Clients that call "the first one" call the
  user's first agent.

### Pairing directory (optional)

The project maintains a public directory at `https://wristcall-pair.trigram.com.br`; anyone can run
their own (see `directory/README.md`). When the owner uses a directory, the client resolves the code first:
`GET {directory}/v1/resolve/{code}` → `200 {"url":"https://server"}` (the entry is
deleted) or `404`. On `404` the client should retry up to 3 times, 2 s apart,
because the directory's storage can take a few seconds to
propagate. Then it calls `POST {url}/v1/pair` with the same code.

The code is registered in the directory by the server (`wristcall pair`, flow A) or by the
directory's static page (the owner enters the URL, flow B):

| Route | Body | Responses |
|---|---|---|
| `POST {directory}/v1/codes` | `{"url": "https://server", "code": "12345678"}` (`code` is optional; without it, the directory generates one) | `201 {"code","expires_at"}`. `400 {"error":"invalid_url"\|"invalid_code"\|"bad_request"}`: URL that is not `https://`, code that does not have 8 digits, invalid JSON. `409 {"error":"conflict"}`: code already active (the server generates another and retries). `429 {"error":"rate_limited"}`. `503 {"error":"unavailable"}`: no free code found |
| `GET {directory}/v1/resolve/{code}` | | `200 {"url"}` (the entry is deleted). `404 {"error":"not_found"}` |

Uniqueness and single use are best effort (eventually consistent KV): in
rare races, two simultaneous registrations of the same code or two back to back
resolutions may go through. Security comes from the code validated by the server at
`POST /v1/pair`, not from the directory.

## Call (`WS /v1/call`)

Text frames are JSON control messages. Binary frames are audio.

### Opening

1. The client connects with the `Authorization` header. Invalid token: the server closes
   with code **4401**.
2. Within 10 s, the client sends:
   ```json
   {"type":"session.start","protocol":1,"profile":"default",
    "audio_in":{"codec":"pcm16","sample_rate":16000,"channels":1},"turn_end":"auto"}
   ```
   `agent` (an agent `slug` or `id`) and `profile` (a `slug`, the 0.2.x name) are
   optional; when both are present `agent` wins; when both are absent the call goes
   to the user's first agent. An agent of another user is unknown.
   `turn_end` is optional (absent = the agent's own `turn_end` when `agent` is
   present, `"auto"` otherwise) and chooses, for this call only, how the user's turn ends
   (see [End of the user's turn](#end-of-the-users-turn)). Any value other than
   `"auto"` or `"manual"` (including `null`) is a `bad_message` error.
3. The server answers:
   ```json
   {"type":"session.ready","session_id":"9f2c...","profile":{"name":"default","display_name":"Agent"},
    "agent":{"id":"ag_3f9c0a1b2c3d","slug":"default","display_name":"Agent","icon":"waveform",
             "call_type":"conversation","turn_end":"auto"},
    "turn_end":"auto","audio_out":{"codec":"pcm16","sample_rate":24000,"channels":1}}
   ```
   `agent` is the agent being called and `turn_end` the mode in force for this
   call. `profile` repeats the agent's `slug` and `display_name` (0.2.x shape).
   Opening errors arrive as `error` with `fatal: true`, followed by the
   close with code **4400**.

### Audio

- Client → server: PCM16 little-endian, mono, 16 kHz. 20 ms frames
  (640 bytes) are recommended; the server accepts any even size.
- Server → client: PCM16 little-endian, mono, at the `audio_out` rate, in 20 ms
  frames. The last frame of each response is padded with silence.
- While muted, the client does not send audio.

### End of the user's turn

The turn starts with the first frame the server detects as speech (plus up to
300 ms of audio from just before it; values here are defaults, configurable per
agent). How it ends depends on the `turn_end` of
`session.start`:

| `turn_end` | The turn ends by | `turn.user_end` reasons |
|---|---|---|
| `"auto"` (default for 0.2.x clients and agents set to auto) | 800 ms of silence after speech (VAD; speech shorter than 300 ms followed by silence is dropped as noise), mute, or the duration limit | `"vad"`, `"mute"`, `"limit"` |
| `"manual"` | mute or the duration limit only; silence never ends it, however long (a turn that reaches the limit with less than 300 ms of speech is dropped as noise) | `"mute"`, `"limit"` |

In both modes:
- Mute before any speech ends nothing; while muted, audio is ignored.
- The duration limit (60 s) counts from the start of speech, pauses included.
  It is a safety limit.
- In `"manual"`, every frame from the start of speech until the mute is part of
  the turn, pauses included, even if the speech was short.
- In `"manual"`, if the duration limit is reached and the turn has less than
  300 ms of speech (a blip followed by silence), the server drops it silently,
  as noise: no `turn.user_end`, no transcription, and a new turn starts.

`turn_end` was added in server 0.2.0 without changing the protocol version.
Servers 0.1.x ignore the field (unknown fields are ignored), so a `"manual"`
call to such a server behaves as `"auto"` and may end turns by silence
(`reason: "vad"`). A value the server does not know (for example a future
mode) is a fatal `bad_message` (close 4400). Clients therefore check `version`
in `GET /v1/health` before sending a non-default value.

Since server 0.3.0, when `session.start` names the agent with `agent`, an
absent `turn_end` means that agent's own mode (`"auto"` unless its owner set it
to `"manual"`). Without `agent` (only `profile`, or neither, as 0.2.x clients
send), an absent `turn_end` stays `"auto"`, as in 0.2.0. `session.ready.turn_end`
tells which mode is in force.

### Client messages

| Message | When |
|---|---|
| `{"type":"mute","muted":true}` | the user muted: closes the turn right away if there is already speech (the "over") |
| `{"type":"mute","muted":false}` | the user unmuted: starts a new turn |
| `{"type":"session.end"}` | the user hung up; the client then closes the WebSocket |

Unknown fields are ignored (future compatibility).

### Server messages

| Message | Meaning |
|---|---|
| `{"type":"turn.user_end","reason":"vad"\|"mute"\|"limit"}` | the user's turn closed: by silence (`"auto"` calls only), by mute or by going over the duration limit (60 s by default, configurable per agent). See [End of the user's turn](#end-of-the-users-turn) |
| `{"type":"transcript","role":"user"\|"assistant","text":"..."}` | text of the turn (informational) |
| `{"type":"turn.agent_start"}` | the response audio is about to start |
| `{"type":"turn.agent_end"}` | all of the response audio has been sent |
| `{"type":"error","code":"...","message":"...","fatal":false}` | failure; with `fatal:true` the server closes right after |

While the agent is responding, the client's audio is discarded. The server only
starts listening again after the estimated playback time of the audio sent plus 200 ms.

### Error codes

| Code | Fatal | Cause |
|---|---|---|
| `bad_message` | at opening, yes; afterwards, no | invalid JSON, unknown type or invalid field (for example an unknown `turn_end`) |
| `not_started` | yes | the first message was not `session.start`, or it did not arrive within 10 s |
| `unsupported_protocol` | yes | `protocol` other than 1 |
| `unsupported_audio` | yes | `audio_in` other than pcm16 16 kHz mono |
| `unknown_profile` | yes | the agent (`agent` or `profile`) does not exist for this device's user, or the user has no agents |
| `agent_unavailable` | yes | the agent exists but cannot be used now (for example, it names a provider the server no longer offers) |
| `stt_failed` | no | transcription failed or timed out |
| `responder_failed` | no | the agent did not answer or stopped midway; if nothing was spoken, the server speaks an apology sentence; if it stopped midway, it keeps what was already said |
| `tts_failed` | no | the voice failed; the response `transcript` still arrives |
| `internal` | no | unexpected server error |

### Close codes

| Code | Reason |
|---|---|
| 1000 | normal end |
| 4400 | fatal protocol error |
| 4401 | missing, invalid or revoked token |

## Agents

An agent is what a call talks to. Its summary, as devices see it:

| Field | Meaning |
|---|---|
| `id` | stable id (`ag_` + 12 hex) |
| `slug` | short name, unique per user: 1 to 32 of `a-z`, `0-9`, `-`, starting with a letter or digit |
| `display_name` | name on the watch (1 to 64 characters) |
| `icon` | SF Symbol name (default `waveform`) |
| `call_type` | `conversation`; `one-shot` and `monologue` are reserved for a later version and refused for now |
| `turn_end` | `auto` or `manual`: the mode used when `session.start` has no `turn_end` |

The owner also sees (and sets) `position` (order on the watch; setting it moves
the agent to that index and renumbers the rest), `language`, `stt` (input),
`action` (the model that answers), `tts` (output), `system_prompt`,
`fallback_message`, `vad` (`silence_ms` and the other turn settings) and
`timeouts`. `stt`, `action` and `tts` are either `{"provider":"<name>"}`, a
provider the server offers (`GET /v1/providers`), or the user's own service:
`{"type":"openai_stt"|"openai_chat"|"openai_tts", "base_url": ..., "model": ..., ...}`,
accepted only when the server allows custom endpoints. `base_url` must be an
`http(s)` URL without credentials, query or fragment (secrets go in options such as
`api_key`). Options whose name has a word like `key`, `token`, `secret`, `password`
or `authorization`, at any depth (`extra_body`, `extra_form`), are returned as
`"***"`; sending `"***"` back in an update of the same `type` keeps the stored value,
and `"***"` anywhere else is `invalid`. `vad` and `timeouts` values are bounded
(for example `silence_ms` 100 to 10000, `max_turn_ms` up to 300000, timeouts up to
120 s).

Custom endpoints make the server send requests to URLs that users choose, including
addresses inside the server's own network (SSRF). They are on by default for a
self-hosted server whose users are trusted; on a server with users you do not
trust, set `limits.custom_endpoints: false` so agents can only use the providers
the operator offers.

## Management API

`Authorization: Bearer <API token>` (`wc_pat_...`, created with
`wristcall users tokens add`). Everything is scoped to the token's user. A device
token may only read `GET /v1/agents` and `GET /v1/agents/{ref}` (summary view);
anything else answers `403 {"error":"forbidden"}`. Errors are
`{"error": code, "message": text}`.
A body that is not valid JSON or not a JSON object answers `422 {"error":"invalid"}`.

| Route | Body | Responses |
|---|---|---|
| `GET /v1/agents` | | `200 {"agents":[agent]}` in the watch's order |
| `POST /v1/agents` | agent fields; `slug` required; absent `stt`/`action`/`tts` = the server's only provider of that kind | `201 agent`. `422 invalid\|unsupported`. `409 conflict` (slug taken). `403 limit` |
| `GET /v1/agents/{ref}` | | `200 agent`. `404 not_found`. `ref` is a `slug` or an `id` |
| `PATCH /v1/agents/{ref}` | fields to change; `vad` and `timeouts` merge key by key | `200 agent`. `404`, `409`, `422` |
| `DELETE /v1/agents/{ref}` | | `204`. `404` |
| `GET /v1/providers` | | `200 {"providers":[{"name","kind":"stt"\|"action"\|"tts"}],"custom_endpoints":true}` |
| `GET /v1/devices` | | `200 {"devices":[{"id","name","created_at"}]}` |
| `DELETE /v1/devices/{id}` | | `204`. `404` |
| `POST /v1/pairing-codes` | | `201 {"code","expires_at","server_url","via_directory"}` (+ `"warning"` when the directory was unreachable). `403 limit` (device limit). `502 directory` |

Missing or invalid token: `401 {"error":"unauthorized"}`.

## Versioning

Compatible changes (new fields, new messages the client can ignore)
keep version 1. Incompatible changes become version 2, negotiated by the
`protocol` field of `session.start`.
