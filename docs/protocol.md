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
| `GET /v1/health` | | `200 {"status":"ok","version":"0.1.0","protocol":1}` |
| `POST /v1/pair` | `{"code": "12345678" \| null, "device_name": "Apple Watch"}` | `200 {"device_id","token"}`: paired (flow A). `202 {"request_id","poll_token","expires_at"}`: waiting for the owner's approval (flow B). `401 {"error":"invalid_code"}`. `429 {"error":"rate_limited"}` |
| `POST /v1/pair/poll` | `{"poll_token": "..."}` | `202 {"request_id","expires_at"}`: pending. `200 {"device_id","token"}`: approved (delivered only once). `410 {"error":"gone"}`: expired or already delivered. `422`: body without `poll_token` or with more than 128 characters |
| `GET /v1/me` | | `200 {"device_id","device_name","profiles":[{"name","display_name"}]}`. `401` |
| `DELETE /v1/me` | | `204`: token revoked. `401` |

Rules:
- The code has 8 digits; spaces and hyphens are ignored. It is valid for 10 minutes, once.
- In manual mode, `401` is also returned when there are too many pending requests (try again later). An invalid body returns `422`.
- In flow B, the client shows `request_id` (4 digits) so the owner can approve it with
  `wristcall devices approve <request_id>`, and polls `POST /v1/pair/poll` every
  2 s with the `poll_token` in the body. The `poll_token` is a client secret; never
  display it or put it in the URL (the path shows up in access logs).
- `expires_at` is epoch in seconds (float).

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
    "audio_in":{"codec":"pcm16","sample_rate":16000,"channels":1}}
   ```
   `profile` is optional (absent = `default`).
3. The server answers:
   ```json
   {"type":"session.ready","session_id":"9f2c...","profile":{"name":"default","display_name":"Agent"},
    "audio_out":{"codec":"pcm16","sample_rate":24000,"channels":1}}
   ```
   Opening errors arrive as `error` with `fatal: true`, followed by the
   close with code **4400**.

### Audio

- Client → server: PCM16 little-endian, mono, 16 kHz. 20 ms frames
  (640 bytes) are recommended; the server accepts any even size.
- Server → client: PCM16 little-endian, mono, at the `audio_out` rate, in 20 ms
  frames. The last frame of each response is padded with silence.
- While muted, the client does not send audio.

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
| `{"type":"turn.user_end","reason":"vad"\|"mute"\|"limit"}` | the user's turn closed: by silence, by mute or by going over 60 s (default, configurable per profile) |
| `{"type":"transcript","role":"user"\|"assistant","text":"..."}` | text of the turn (informational) |
| `{"type":"turn.agent_start"}` | the response audio is about to start |
| `{"type":"turn.agent_end"}` | all of the response audio has been sent |
| `{"type":"error","code":"...","message":"...","fatal":false}` | failure; with `fatal:true` the server closes right after |

While the agent is responding, the client's audio is discarded. The server only
starts listening again after the estimated playback time of the audio sent plus 200 ms.

### Error codes

| Code | Fatal | Cause |
|---|---|---|
| `bad_message` | at opening, yes; afterwards, no | invalid JSON or unknown type in the middle of the call |
| `not_started` | yes | the first message was not `session.start`, or it did not arrive within 10 s |
| `unsupported_protocol` | yes | `protocol` other than 1 |
| `unsupported_audio` | yes | `audio_in` other than pcm16 16 kHz mono |
| `unknown_profile` | yes | the profile does not exist on the server |
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

## Versioning

Compatible changes (new fields, new messages the client can ignore)
keep version 1. Incompatible changes become version 2, negotiated by the
`protocol` field of `session.start`.
