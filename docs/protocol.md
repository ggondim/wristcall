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
| `GET /v1/health` | | `200 {"status":"ok","version":"0.6.0","protocol":1,"account":null,"push":null}`. `account` is `{"issuer","device_credential"}` when the server accepts a [central account](#central-account-optional), `null` otherwise; `push` is `{"relay"}` when the server sends [push notifications](#push-notifications-optional), `null` otherwise (older servers omit either) |
| `POST /v1/pair` | `{"code": "12345678" \| null, "device_name": "Apple Watch"}` | `200 {"device_id","token"}`: paired (flow A). `202 {"request_id","poll_token","expires_at"}`: waiting for the owner's approval (flow B). `401 {"error":"invalid_code"}`. `429 {"error":"rate_limited"}` |
| `POST /v1/pair/poll` | `{"poll_token": "..."}` | `202 {"request_id","expires_at"}`: pending. `200 {"device_id","token"}`: approved (delivered only once). `403 {"error":"limit"}`: approved, but the user is at the device limit, so the device cannot be collected yet (the request stays valid; try again after a device is revoked). `410 {"error":"gone"}`: expired, denied or already delivered. `422`: body without `poll_token` or with more than 128 characters |
| `GET /v1/calls/{call_id}` | | `200 call` (see [One-way calls](#one-way-calls-one-shot-monologue)): a call of this token's user, with a device or an API token; a device token reads only the calls made from that device. `404 {"error":"not_found"}` (also for a call made from another device). `401` |
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

## Central account (optional)

A server can accept the login of a central account (OIDC, for example the one run for the
project's own apps) as a way to pair watches and to link a user, so nobody types an 8 digit code. It
is off unless the operator sets `central_account` in `wristcall.yaml`; without it the server
behaves like 0.4.0 and every route below answers `404 {"error":"not_configured"}`
(`POST /v1/pair/account` spends from the per-IP budget before it answers that).

Clients read `account` in `GET /v1/health`: `{"issuer","device_credential"}` or `null`.
`device_credential` is `"approval"` or `"attestation"` (see [Configuration](../README.md#central-account-optional)).
`issuer` is the wristcall Cloud the server trusts (0.6.0+). The client logs in at the central account with its
own `client_id` (on a watch, with the device authorization grant, RFC 8628), then asks the Cloud for a token
made for this server only:

1. The client reads `account.issuer` in `GET /v1/health` and compares it with the Cloud URL of its own configuration
   (the reference client's `--cloud`, an app's build settings). If they differ, it refuses the server and sends
   nothing: a server announcing a "Cloud" of its own would otherwise collect login tokens that are good at the real
   Cloud.
2. `POST {cloud}/v1/server-tokens {"audience": "<server URL>"}` with `Authorization: Bearer <login token>`, to the
   Cloud of its configuration. The audience is the exact URL the client itself uses to reach the server, never one
   the server announces. The Cloud answers `200 {"token","audience","expires_at"}`: a token valid for 5 minutes
   (see [cloud/README.md](../cloud/README.md#per-server-tokens)).
3. The client sends that token to `POST /v1/pair/account` or `POST /v1/account/link` of that server, and to no other.

The server accepts only these per-server tokens: header `typ` `wc-server+jwt`, `iss` = its `issuer`, `aud` =
one of its `audience` URLs. A token made for another server, or the central account's own access token, is a
`401`. The token is **not** a credential of this server either: it does not authenticate the management API,
calls or `GET /v1/me`; it only works as proof of who the person is, in `POST /v1/pair/account` and
`POST /v1/account/link`. ID tokens are refused, and when the server sets `clients`, the token's `client_id`
or `azp` (the app the person logged in with) must be one of them.

Why: the same login works on every server that trusts the issuer. If the token were enough to
manage a server, a token handed to one server's operator would manage all of them. A local user
therefore has to be linked first, with a second proof that only this server can give (an API token
or a pairing code), and the token only opens the door to pairing.

### Linking a user

| Route | Body | Responses |
|---|---|---|
| `POST /v1/account/link` | `{"token": "<per-server token>"}` with `Authorization: Bearer <API token>`, or `{"token": "...", "code": "12345678"}` without `Authorization` | With an API token: `200 {"linked":true,"issuer"}` (a `code` sent along is ignored and not spent). With a code: `200 {"linked":true,"issuer","user":{"id","handle"},"api_token"}`. Errors below |
| `DELETE /v1/account/link` | | `204`: the user's link removed. `404 {"error":"not_found"}`: not linked. `401`, `403 forbidden` (device token) |

Errors of `POST /v1/account/link`, in the order they are checked. Like every error of this API, the body is
`{"error": <code>, "message": <text>}`:

| Status | `error` | When |
|---|---|---|
| `404` | `not_configured` | the server has no `central_account` |
| `429` | `rate_limited` | more than 10 requests a minute from one IP (the budget is shared with `POST /v1/pair` and `POST /v1/pair/account`) |
| `401` | `unauthorized` | `Authorization` sent but not a valid token |
| `403` | `forbidden` | `Authorization` is a device token |
| `422` | `invalid` | body is not a JSON object, `token` missing or empty, or `code` not a string |
| `401` | `unauthorized` | no `Authorization` and no `code` |
| `401` | `invalid_account_token` | signature, issuer, audience, expiry, `client_id` or token type is wrong (the `message` says which) |
| `503` | `account_unavailable` | the issuer or its keys cannot be reached and nothing is cached; try again later |
| `401` | `invalid_code` | code wrong, expired or already used (a wrong code counts as a failed attempt, as in `POST /v1/pair`; five burn the code) |
| `409` | `conflict` | this central account is already linked to another user of the server |

Rules:
- A user links one central account; one central account links one user per server. Linking again with the same user
  replaces the previous link.
- The central token is checked before the code is claimed, so a bad token or an unreachable issuer
  does not spend the code. A code is spent as soon as it is claimed, even when the link then fails
  with `409 conflict`: ask for a new one.
- **A pairing code now also grants management.** Linking with a code returns `api_token`, a new
  API token (named `account link`) of the code's user. Whoever holds a valid central login and
  sees a code first, for example a code shown on a screen, links to that user and gets a full management
  token (agents, devices, webhooks, unlinking). Treat the code as a secret until it is used. A token
  created this way can be revoked with `wristcall users tokens revoke`.
- `DELETE /v1/account/link` or `wristcall users unlink <handle>` removes the link. Devices already
  paired stay paired.

### Pairing with the account

| Route | Body | Responses |
|---|---|---|
| `POST /v1/pair/account` | `{"token": "<per-server token>", "device_name": "Apple Watch"}` (no `Authorization`) | `200 {"device_id","token"}`: paired (`attestation`). `202 {"request_id","poll_token","expires_at"}`: waiting for the user's approval (`approval`). Errors below |

| Status | `error` | When |
|---|---|---|
| `422` | | body without `token`, or `device_name` over 64 characters |
| `429` | `rate_limited` | per-IP budget (10 a minute, shared with `POST /v1/pair` and `POST /v1/account/link`); checked first, even when the server has no central account |
| `404` | `not_configured` | the server has no `central_account` |
| `401` | `invalid_account_token` | the token is not acceptable (same reasons as above) |
| `503` | `account_unavailable` | the issuer cannot be reached |
| `403` | `not_linked` | no user of the server is linked to this central account; link one first |
| `403` | `limit` | the user is at the device limit (`limits.max_devices_per_user`) |
| `429` | `too_many_requests` | too many pending requests for this user (5); approve or deny them first |

In `approval` mode the client then polls `POST /v1/pair/poll` with the `poll_token`, exactly as in flow B, and
gets `200 {"device_id","token"}` once approved. A denied request answers `410 {"error":"gone"}`;
an approved one answers `403 {"error":"limit"}` while the user is at the device limit (the limit is checked again
when the device is collected, so approving cannot get around it).

### Approving requests

Requests created by `POST /v1/pair/account` in `approval` mode are aimed at one user. Only that user
can see, approve or deny them, with an **API token** (never the central token: the login that
asks for a device cannot also approve it).

| Route | Body | Responses |
|---|---|---|
| `GET /v1/pairing-requests` | | `200 {"requests":[{"request_id","device_name","expires_at"}]}`: pending requests aimed at the token's user |
| `POST /v1/pairing-requests/{request_id}/approve` | | `200 {"device_name"}`. `404 {"error":"not_found"}`: no such pending request aimed at this user. `403 {"error":"limit"}`: device limit |
| `POST /v1/pairing-requests/{request_id}/deny` | | `204`. `404 {"error":"not_found"}` |

All three answer `404 not_configured` without `central_account`, `401 unauthorized` without a valid token and
`403 forbidden` with a device token. Requests without a target (flow B of `pairing_approval: manual`) are not listed
here and cannot be approved or denied through this API: the operator uses `wristcall devices approve|deny`.
A request lives 10 minutes. Each user can have 5 pending at once.

### Revocation and limits

- The server only checks the token's signature and claims against the Cloud's published keys
  (cached for 1 hour; kept if the Cloud goes down; at most one key fetch a minute). It never asks the
  Cloud whether a login is still valid. A per-server token lives 5 minutes, and the Cloud issues one for as long as
  the central account's access token is valid, so a session revoked at the central account keeps working until that
  token expires: keep access tokens short (minutes) at the account issuer.
- `unlink` does not revoke devices already paired: use `wristcall devices revoke` or `DELETE /v1/devices/{id}`.
- In `attestation` mode a leaked per-server token of a linked account pairs a device until it expires; revoke the
  device afterwards. `approval` mode puts the user between the token and the device.
- A per-server token works only at the server whose URL it names: a server operator who receives one cannot replay it
  at another server or at the Cloud API (0.5.0 accepted the central account's own token, which could). Links made
  with 0.5.0 named the account issuer and must be made again.

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
| `{"type":"call.captured","call_id":"c_...","reason":"limit"}` | one-way calls only: the server stopped recording by itself and closes the call (1000). See [One-way calls](#one-way-calls-one-shot-monologue) |

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

## One-way calls (one-shot, monologue)

Since server 0.4.0, an agent whose `call_type` is `one-shot` or `monologue` only
listens: no answer, no voice. The server records until the user hangs up,
transcribes, and posts the text to the agent's webhook (its `action`). Clients
may check `version` in `GET /v1/health` (0.4.0 or later) before offering these agents;
older servers refuse to create them. A client may instead rely on the agent list: a
server older than 0.4.0 cannot create one-way agents, so it never lists them (watch
0.2.0 does this).

| | `one-shot` | `monologue` |
|---|---|---|
| For | say one thing ("buy milk") and hang up | talk until you hang up (ideas, notes) |
| Recording ends | at hang-up, or at the agent's turn limit (`vad.max_turn_ms`, 60 s by default) | at hang-up, or at `limits.max_one_way_call_s` (30 min by default) |

In both, silence never ends the call, and mute pauses the recording (audio sent
while muted is dropped) without ending anything. A dropped connection counts as
hanging up: what was recorded is still transcribed and delivered.

The call goes like this:

1. `session.start` as usual (`turn_end` is ignored). `session.ready` adds `call_id`,
   reports `"turn_end":"manual"`, and its `audio_out` is nominal: no audio comes back.
2. The client streams audio. The server sends nothing back (no `turn.user_end`,
   `transcript` or audio). If it reaches the limit first, it sends `call.captured`
   and closes with 1000.
3. The client hangs up (`session.end`, then close) and asks
   `GET /v1/calls/{call_id}` every 1 to 2 s until the status is final. The
   WebSocket is closed by then: this is plain HTTPS, with the device token (a device token reads only the calls made
   from that device).

`GET /v1/calls/{call_id}` answers (the fields after `finished_at` since server 0.5.0, see [History](#history)):

```json
{"id":"c_5d1f...","agent_id":"ag_3f9c0a1b2c3d","call_type":"one-shot","status":"delivered","error":null,"text":"buy milk","attempts":1,"last_http_status":204,"created_at":1760000000.0,"ended_at":1760000004.2,"finished_at":1760000005.1,"agent":{"id":"ag_3f9c0a1b2c3d","slug":"note","display_name":"Note"},"expires_at":null,"entries":[{"role":"user","text":"buy milk","error":null,"at":1760000004.9}]}
```

| `status` | Final | Meaning |
|---|---|---|
| `recording` | no | the call is open |
| `processing` | no | hung up; transcribing, then delivering |
| `delivered` | yes | the webhook answered `2xx` |
| `failed` | yes | see `error` |
| `empty` | yes | nothing was said (no speech, or a blank transcript); nothing is delivered |
| `ended` | yes | a conversation call closed (see [History](#history)) |

| `error` | Meaning |
|---|---|
| `stt_failed` | a piece of the recording could not be transcribed (two tries); `text` keeps what was; nothing is delivered |
| `delivery_failed` | three attempts without a `2xx`; `text`, `attempts` and `last_http_status` (null after a timeout or connection error) tell what happened |
| `interrupted` | the server stopped while processing; `text` is kept if it was ready |
| `internal` | unexpected server error |

Text is kept after delivery too: every call is part of its agent's [history](#history). A call that ended
`delivery_failed`, or `interrupted` with its text, can be delivered again (`POST /v1/calls/{id}/redeliver`).

### Delivery to the webhook

After transcribing, the server sends `POST <url>` with the agent's `headers` and:

```json
{"event":"call.completed","version":1,"call_id":"c_5d1f...","call_type":"one-shot","agent":{"id":"ag_3f9c0a1b2c3d","slug":"note","display_name":"Note"},"language":"pt","text":"buy milk","started_at":"2026-10-09T05:40:00Z","ended_at":"2026-10-09T05:40:04Z"}
```

- Headers set by the server: `Content-Type: application/json`, `User-Agent: wristcall/<version>`
  and `Idempotency-Key: <call_id>` (the same on every attempt: drop repeats).
- Any `2xx` means delivered; the response body is ignored. Redirects are not followed.
- Up to three attempts (one, then retries after 3 s and 6 s), 15 s each: about a
  minute at most. Fields may be added within `version` 1; receivers ignore unknown ones.
- Transcription comes first and is not part of that minute: each piece of the recording
  gets up to two tries of the agent's `timeouts.stt_s` (10 s by default). With the
  defaults, a one-shot reaches its final status within about 75 s of hanging up.

A watch app older than 0.4.0 (for example watch 0.1.0) can call a one-way agent:
it records, the call ends normally, and the delivery happens; it just never shows
the result.

Watch 0.2.0 and later sends both `agent` and `profile` in `session.start`, keeps the `call_id` of a one-way
call and, after it ends, polls `GET /v1/calls/{call_id}` every 1.5 s for up to 3 minutes while the app is open.
With a server that sends [push notifications](#push-notifications-optional), the `call.finished` notification
ends that wait at once (watch 0.3.0, push build).

## History

Since server 0.5.0 every call is recorded, delivered or not, conversations included (one per WebSocket session).
Only text is kept, never audio.

- A **conversation**'s `session.ready` also carries `call_id`. Its record is `recording` while the call is open,
  then `ended` (`empty` when nothing was said; `ended` with `error: "interrupted"` if the server stopped during the
  call). Its `entries` alternate what the user said (`role: "user"`) and the agent's answer (`role: "agent"`).
  An entry's `error` tells why its text is missing or partial: `stt_failed` (not understood, `text` null),
  `responder_failed` (no answer, or cut: `text` has what was said), `tts_failed` (the answer was written but not
  spoken), `unreadable` (encrypted with a key this server does not have).
- A **one-way** call has one `user` entry with the transcript. `text` (top level) is what the user said, joined.
- `agent` is the agent as it was at call time (`slug`, `display_name`): an agent can be renamed or deleted and its
  history stays. Deleting an agent keeps its calls.
- `expires_at`: when the server deletes the call (Unix seconds), from the agent's `retention_days` within the
  operator's ceiling (see [Agents](#agents)); `null` keeps it until the user deletes it. Expired calls are deleted
  within the hour.

With the user's API token (a device token answers `403 forbidden`, except for `GET /v1/calls/{id}`):

| Route | Responses |
|---|---|
| `GET /v1/calls` | `200 {"calls":[call],"next_before":string\|null}`, newest first, each call as `GET /v1/calls/{id}`. Query: `agent` (slug or id, also a deleted agent's id), `q` (search), `since` and `until` (Unix seconds or ISO 8601; without an offset, UTC; `until` excluded), `limit` (1 to 100, default 50), `before` (the `next_before` of the previous page: an opaque position, still valid if that call was deleted meanwhile; a full page can return a `next_before` that leads to an empty page). `404 not_found` (agent). `422 {"error":"invalid"}` for a bad `since`, `until` or `before`; a `limit` outside 1 to 100 or a `q` over 500 characters gets the standard validation `422` (`{"detail":[...]}`) |
| `GET /v1/calls/{id}` | `200 call` (device or API token; a device token reads only the calls made from that device). `404` (also for another device's call) |
| `DELETE /v1/calls/{id}` | `204`. `404` |
| `DELETE /v1/calls?agent=<ref>` or `?all=true` | `200 {"deleted":n}`. `422` with neither or both |
| `GET /v1/calls/export?format=md\|json` | `200`, a file (`Content-Disposition: attachment`): Markdown to read, or JSON `{"version":1,"exported_at","calls":[call]}`. Same `agent`, `since`, `until`; every matching call, newest first, times in UTC |
| `POST /v1/calls/{id}/redeliver` | `202 call` (now `processing`; poll `GET /v1/calls/{id}`). `409` with `error`: `not_failed` (only `delivery_failed`, or `interrupted` with text), `busy`, `no_text`, `not_one_way`, `agent_gone` (agent deleted), `agent_unavailable` |

Search (`q`) matches whole words, all of them in the same call (in what the user said or in the agent's answers),
ignoring case and accents: `reuniao` finds "Reunião". No prefixes, phrases or operators: anything that is not a
letter or a digit separates words; at most 16 words count (the rest are ignored) and each word is cut at 64
characters. An empty `q` does not filter, but a `q` of only punctuation finds nothing. Redelivery sends the kept text to the agent's webhook as it is configured now, with the same
`Idempotency-Key` (the call id) and three more attempts; `attempts` adds them up.

## Push notifications (optional)

Since server 0.6.0 a server can notify its clients through a push relay, the wristcall Cloud (see
[cloud/README.md](../cloud/README.md#push-relay)). It is off unless the operator sets `push.relay_url` in
`wristcall.yaml`; without it `GET /v1/health` shows `"push": null` and the routes below answer
`404 {"error":"not_configured"}`. With it, `push` is `{"relay": "<relay URL>"}`.

The client knows the relay URL from its own configuration, never from a server: if `push.relay` names another relay
(or is `null`), the client skips push for that server and registers nothing. Otherwise:

1. The client registers its APNs token or Web Push subscription at the relay
   (`POST {relay}/v1/push/registrations`, anonymous) with a `label` (the watch uses the server's host), a `tag`
   (opaque, up to 64 characters: the watch uses its local id of the server) and the `events` it wants. It gets a
   push key, `wc_push_` and 43 characters. One key per pair of client and server: the relay forces the label and the
   tag on every message of that key, so the client knows which server a notification came from without trusting the
   server.
2. It hands the key to the server with `PUT /v1/push`.
3. Each time it starts (or comes back to the foreground), it checks the key at the relay
   (`GET {relay}/v1/push/registrations/current`): on `410` it registers again and repeats `PUT /v1/push`, which is
   idempotent. A new APNs token or subscription means new keys; the old ones are dropped at the relay.

| Route | Auth | Body | Responses |
|---|---|---|---|
| `PUT /v1/push` | device or API token | `{"push_key": "wc_push_..."}` | `204`: kept, one key per paired device or API token (a new one replaces the old, which the server drops at the relay). `422 {"error":"invalid"}`: not a push key. `401`. `404 not_configured` |
| `DELETE /v1/push` | device or API token | | `204`: removed (and dropped at the relay). `404 {"error":"not_found"}`: no key. `401`. `404 not_configured` |

The key is a secret: the server never logs it or sends it back, and the body is never echoed. Revoking the device or
the API token deletes its key.

What the server sends (the relay adds the label as the notification's subtitle and the tag as `wristcall.tag`):

| Event | To | Title, body | `data` |
|---|---|---|---|
| `call.finished` | the device that made a one-shot or monologue call, when it reaches its final status (`delivered`, `failed`, `empty`) | the outcome ("Delivered", "Not delivered", "Not transcribed", "Nothing heard", "Call failed") and a sentence with the agent's name | `{"call_id","status","error","agent_id"}`, as in `GET /v1/calls/{call_id}`; the call id is also the collapse id |
| `device.approval` | the management apps (keys set with an API token) of the user a `POST /v1/pair/account` request in `approval` mode is aimed at | "New device" and the device's name | `{"request_id","device_name","expires_at"}` |

On APNs the message arrives as:

```json
{"aps":{"alert":{"title":"Delivered","subtitle":"wristcall.example.com","body":"Note got your message."},"sound":"default","thread-id":"call.finished","category":"WC_CALL_FINISHED"},
 "wristcall":{"v":1,"event":"call.finished","tag":"<the registration's tag>","data":{"call_id":"c_5d1f...","status":"delivered","error":null,"agent_id":"ag_3f9c0a1b2c3d"}}}
```

- **Privacy.** The title, body and label pass through the relay and then Apple or the browser's push service. The
  transcript never does: a notification says how the call ended and to which agent, and the client reads the text
  from the server (`GET /v1/calls/{call_id}`).
- **Best effort.** The call's status and the pairing request do not wait for the relay: a relay that is down, slow or
  refusing only shows in the server's log. A key the relay answers `410` for is forgotten by the server; the client
  finds out at its next check and registers again. Clients still poll while they are open; a notification only
  ends the wait earlier.
- Requests in `pairing_approval: manual` mode (no target user) notify nobody: the operator approves them in the CLI.

## Agents

An agent is what a call talks to. Its summary, as devices see it:

| Field | Meaning |
|---|---|
| `id` | stable id (`ag_` + 12 hex) |
| `slug` | short name, unique per user: 1 to 32 of `a-z`, `0-9`, `-`, starting with a letter or digit |
| `display_name` | name on the watch (1 to 64 characters) |
| `icon` | SF Symbol name (default `waveform`) |
| `call_type` | `conversation` (talk and listen), `one-shot` or `monologue` (only listens; see [One-way calls](#one-way-calls-one-shot-monologue)) |
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
120 s). `retention_days` (since 0.5.0) is how long the agent's calls stay in the [history](#history): a number of
days (1 to 36500), `"forever"`, or `null` for the operator's default; the owner's view also has
`effective_retention_days`, the one in force within the operator's ceiling (`null`: kept until deleted). Changing it
also moves the expiry of the agent's past calls.

A one-way agent's `action` is a webhook: a provider of kind `webhook` or
`{"type":"webhook","url":"https://...","headers":{"Authorization":"Bearer ..."}}`
(`url` without credentials, query or fragment; up to 16 `headers`). Every value of
`headers` comes back as `"***"`, whatever the header name, and sending `"***"` back
keeps the stored value. The server sets `Content-Type`, `Content-Length`, `Host`,
`User-Agent`, `Idempotency-Key`, `Transfer-Encoding`, `Connection` and `Expect`
itself, so `headers` cannot contain them. It has no `tts`
(`null`): one kept from a conversation agent stays stored for switching back,
and an update with `"tts": null` drops it. Changing `call_type` needs an `action`
of the matching kind in the same update; switching to `conversation` also needs a
`tts`, unless one is still stored.

Custom endpoints make the server send requests to URLs that users choose, including
addresses inside the server's own network (SSRF). They are on by default for a
self-hosted server whose users are trusted; on a server with users you do not
trust, set `limits.custom_endpoints: false` so agents can only use the providers
the operator offers. A custom endpoint may only use the provider types listed in
`limits.custom_endpoint_types` (default `openai_stt`, `openai_chat`, `openai_tts`,
`webhook`). A webhook is the easiest probe of all: any user can make the server POST
to an internal address and read the status code back in `GET /v1/calls/{id}`; on a
server shared with people you do not trust, remove `webhook` from that list (or turn
custom endpoints off).

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
| `GET /v1/providers` | | `200 {"providers":[{"name","kind":"stt"\|"action"\|"tts"\|"webhook"}],"custom_endpoints":true}` (`webhook`: the action of one-way agents) |
| `GET /v1/devices` | | `200 {"devices":[{"id","name","created_at"}]}` |
| `DELETE /v1/devices/{id}` | | `204`. `404` |
| `POST /v1/pairing-codes` | | `201 {"code","expires_at","server_url","via_directory"}` (+ `"warning"` when the directory was unreachable). `403 limit` (device limit). `502 directory` |
| `POST /v1/account/link` | see [Linking a user](#linking-a-user) | `200`, `401`, `403` (device token), `404`, `409`, `422`, `429`, `503` |
| `DELETE /v1/account/link` | | `204`. `404` |
| `GET /v1/pairing-requests` | | `200 {"requests":[...]}` |
| `POST /v1/pairing-requests/{id}/approve` | | `200 {"device_name"}`. `403 limit`. `404` |
| `POST /v1/pairing-requests/{id}/deny` | | `204`. `404` |
| `/v1/calls...` | | the call history: see [History](#history) |
| `PUT /v1/push`, `DELETE /v1/push` | see [Push notifications](#push-notifications-optional) | `204`, `401`, `404`, `422` |

Missing or invalid token: `401 {"error":"unauthorized"}`. The central account routes answer `404 not_configured` when the server has no `central_account`, and the push routes when it has no `push`.

## Versioning

Compatible changes (new fields, new messages the client can ignore)
keep version 1. Incompatible changes become version 2, negotiated by the
`protocol` field of `session.start`.
