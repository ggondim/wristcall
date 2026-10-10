# wristcall

Call an AI agent from your Apple Watch. The app uses the native watchOS call
screen (CallKit): you talk, tap mute when you are done (or just pause) and the
agent answers out loud.

This repository has the **server**, which you host. It receives the audio from
the watch, transcribes it, asks your agent and sends back the spoken answer. Each
stage is a provider you can swap through configuration: any OpenAI compatible
API works (OpenAI, Speaches, openedai-speech, LiteLLM, Ollama, vLLM).

Status: server, reference client, pairing directory, watch app and iPhone app are ready. Both apps are
built and installed with Xcode; see [watch/README.md](watch/README.md).

## Run the server in 5 minutes

You need Docker, a domain pointing to the machine and ports 80 and 443 open.

1. Clone and copy the examples:
   ```bash
   git clone https://github.com/ggondim/wristcall && cd wristcall
   cp wristcall.example.yaml wristcall.yaml
   ```
2. Create a `.env`:
   ```bash
   WRISTCALL_DOMAIN=wristcall.yourdomain.com
   OPENAI_API_KEY=sk-...
   CHAT_MODEL=<chat model of your account>
   ```
3. Start it:
   ```bash
   docker compose -f docker-compose.example.yml up -d --build
   curl https://wristcall.yourdomain.com/v1/health
   ```
4. Generate the pairing code and type it on the watch:
   ```bash
   docker compose -f docker-compose.example.yml exec wristcall wristcall pair
   ```

No API key? Put the `demo` agent first (`wristcall agents edit demo --position 0`): it
echoes what was "heard" with a tone instead of a voice, so you can test the whole path.

## Test without the watch

The reference client speaks the same protocol as the watch, using the Mac's microphone:

```bash
python3 -m venv .venv && .venv/bin/pip install -e 'tools/refclient[mic]'
.venv/bin/wristcall-refclient pair --server https://wristcall.yourdomain.com --code 12345678
.venv/bin/wristcall-refclient call
```

With a central account (the server needs `central_account` configured), sign in once with the
device flow and pair without a code. The issuer and client id are the ones your operator registered
(for example `https://auth.trigram.com.br` and the watch app's public client); `--cloud` is the wristcall
Cloud the server trusts (its `central_account.issuer`), which turns the login into a token for that server only:

```bash
.venv/bin/wristcall-refclient login --issuer https://auth.trigram.com.br --client-id <client-id>
.venv/bin/wristcall-refclient pair-account --server https://wristcall.yourdomain.com --cloud <cloud-url>
```

Use headphones. Enter toggles mute.

## Configuration

The operator's settings live in `wristcall.yaml` (see `wristcall.example.yaml`); users,
watches and agents live in the server's database (`data_dir/wristcall.db`).

- `providers`: the STT, chat and TTS services this server offers, each one with a `type`
  and options. Types: `openai_stt`, `openai_chat`, `openai_tts`, `webhook` (where one-shot
  and monologue agents deliver their text), and the fake ones `fake_stt`, `echo_chat`, `tone_tts`.
- `limits`: `max_agents_per_user` (20), `max_devices_per_user` (10) and
  `custom_endpoints` (`true`: agents may use their own STT/chat/TTS URLs; turn it off on a
  server with users you do not trust, since the server would request any URL they give) and
  `custom_endpoint_types` (the provider types a custom endpoint may use; default `openai_stt`,
  `openai_chat`, `openai_tts`, `webhook`) and `max_one_way_call_s` (1800: the longest
  one-shot or monologue call). With users you do not trust, also drop `webhook` from the
  list: it lets them make the server POST to internal addresses and see the status code.
- `profiles` (optional, the 0.2.x format): on the first start they become agents of a user
  called `owner`, `default` first. After that the section is ignored; manage agents with
  `wristcall agents`.
- `server.directory_url`: directory that exchanges the 8 digit code for the server
  URL. `wristcall.example.yaml` already points to the project's public directory (`https://wristcall-pair.trigram.com.br`); without it, the watch
  types the URL once.
- `server.pairing_approval: manual` accepts pairing requests without a code and
  asks for approval with `wristcall devices approve <id>`.
- `providers.<name>.warmup` (STT and TTS only): warms up models that unload when
  idle. `on_call: true` warms up at the start of each call, while you speak;
  `on_start: true` warms up when the server starts; `every_s: 240` keeps the model
  always loaded (uses GPU memory). Example: `warmup: {on_call: true}`.
- Behind a proxy: the server trusts `X-Forwarded-For` only from the IPs in `FORWARDED_ALLOW_IPS` (the example compose already sets it). Behind Cloudflare, use `server.client_ip_header: CF-Connecting-IP`.
- `history`: how long calls stay and whether their text is encrypted (see [Call history](#call-history)).
- `central_account`: pairing with a login of the central account (see [Central account](#central-account-optional)).
- `push`: notifications through the wristcall Cloud relay (see [Push notifications](#push-notifications-optional)).

## Call history

Every call is kept as text (never audio): what the user said, the agent's answers in a conversation, and how a
one-shot or monologue delivery went. Users list, search, export and delete their calls with the API
([docs/protocol.md](docs/protocol.md#history)) or `wristcall history`.

```yaml
history:
  default_retention_days: 90     # when an agent does not choose; absent or null = keep until deleted
  max_retention_days: 365        # ceiling for every agent, "forever" included; needs default_retention_days
  encryption_key: ${WRISTCALL_HISTORY_KEY}   # optional: encrypts the text at rest (wristcall history new-key)
```

- Retention: each agent may set `retention_days` (`wristcall agents edit note --retention 30`, `forever`, or
  `default`); the server deletes expired calls at start and then every `purge_every_s` (default 3600 seconds, an
  optional `history` setting). Without a `history` section the operator sets no default and no ceiling: calls are
  kept until deleted, unless an agent sets its own `retention_days`. Deleting an agent keeps its calls; `wristcall history clear --agent <id>` removes them.
- Turning retention on (or lowering it) applies to past calls at the next start: they are deleted then.
- Encryption: with `encryption_key`, new text is stored with AES-256-GCM and searched through keyed hashes of its
  words (the database shows which entries share a word, not the word). The server records which key it uses and
  refuses to start with another one, or without one, once something was encrypted: **losing the key loses the
  history.** To turn it on, stop the server, add the key, run `wristcall history encrypt` (it encrypts calls
  made before) and start again; to turn it off, stop the server, run `wristcall history decrypt` (with the key still
  set), remove the key and start again.
- Lost key: the server does not start. To start over without the encrypted history, with the server stopped:
  `sqlite3 <data_dir>/wristcall.db "DELETE FROM call_entries WHERE sealed = 1; DELETE FROM meta WHERE key = 'history_key_id';"`
- Search finds calls with all the given whole words, ignoring case and accents, in the user's speech and in the
  agent's answers.
- Deleted and re-encrypted text is overwritten in the database file (`secure_delete`); `encrypt` and `decrypt` also
  compact the file, so no clear text is left behind.
- Search words never reach the server's access log (the query of `/v1/calls` requests is dropped from it), but a proxy
  in front (Traefik, nginx) records them if its own access log is on.

```bash
wristcall history list --search "milk"                  # newest first
wristcall history show c_5d1f0a2b3c4d5e6f
wristcall history export --format md --since 2026-10-01 -o october.md
wristcall history redeliver c_5d1f0a2b3c4d5e6f          # a one-shot whose webhook failed
wristcall history rm c_5d1f0a2b3c4d5e6f
```

## Central account (optional)

`central_account` lets a watch or app pair with a login of the project's central account (OIDC)
instead of an 8 digit code. Without it the server works as before and the routes below answer
`404 not_configured`.

```yaml
central_account:
  issuer: https://cloud.wristcall.example        # the wristcall Cloud; https, http only for localhost
  audience: ["https://wristcall.example.com"]    # this server's URL(s), exactly as apps reach it
  clients: ["<watch client id>", "<iOS client id>"]   # optional: app client ids
  device_credential: approval                    # or attestation
```

- `issuer`: the wristcall Cloud. Apps sign in to the central account and exchange that login at the Cloud
  for a short token made for this server only; the server reads the Cloud's public keys
  (`/.well-known/openid-configuration`) and never calls it with a user token.
- `audience` (required): the URL(s) apps use to reach this server (`https`; `http` only for localhost, and then
  every item must be a loopback URL). A token made for another server, or the central account's own login token,
  is refused. Starting with 0.6.0 a `central_account` without `audience` does not start: point `issuer` at the
  Cloud, add `audience`, and link users again (links made with 0.5.0 named the account issuer).
- `clients` (optional): the client ids of the apps whose logins you accept. Use the apps' client ids, never
  the id of the project: that would accept every app of the project. Absent: any app of the central account.
- `device_credential` decides what a login can do on this server:
  - `approval` (default): the linked user approves each new device (`wristcall devices approve <id>`
    or `POST /v1/pairing-requests/{id}/approve`).
  - `attestation`: any login linked to a user pairs a device right away.

A user has to be linked first: with an API token (`POST /v1/account/link` from the app, or
the app sends a pairing code from `wristcall pair`). Remove the link with `wristcall users unlink <handle>`.
Routes and error codes: [docs/protocol.md](docs/protocol.md#central-account-optional).

Know before you turn it on:
- A pairing code now also links a login and returns a management API token to whoever links first. Do not
  show codes in public places.
- The server does not ask the Cloud whether a login was revoked: a per-server token lives 5 minutes, and the Cloud
  issues one while the central account's access token is valid. Configure short access tokens at the account issuer.
- `users unlink` does not revoke devices already paired (`wristcall devices revoke`). In `attestation`
  mode a leaked login pairs a device until the token expires.
- A per-server token is made for this server's URL only: its operator cannot replay it at another server or at the
  Cloud API. Apps ask the Cloud of their own configuration, never one a server names.
- Keys of the Cloud are cached for 1 hour (stale keys are used if the Cloud is down; at most one fetch a minute).

## Push notifications (optional)

With `push`, the server notifies its clients through the wristcall Cloud relay: the watch gets a check or a failure
when a one-shot or monologue call ends (even with the app closed), and the user's management apps get a notification
when a device asks to pair with their account (`approval` mode). Without it the server works as before, `/v1/health`
shows `"push": null` and the push routes answer `404 not_configured`.

```yaml
push:
  relay_url: https://cloud.wristcall.example   # the wristcall Cloud; https, http only for localhost
  # timeout_s: 10                              # per notification, at most 30
```

There is nothing else to set on the server: each watch or app registers at the relay and gives the server its own
relay key (`PUT /v1/push`), one per device or API token, which the server never logs. Apps use the relay of their own
configuration: a server that names another one gets no key. Push is best effort: a relay that is down or slow only
shows in the log, and calls and pairing requests never wait for it. Routes and payloads:
[docs/protocol.md](docs/protocol.md#push-notifications-optional).

**Privacy.** A notification's title and body (how the call ended and the agent's name, or the name of the device
asking to pair) and its label (the server's host, set by the device) pass through the Cloud and then through Apple or
the browser's push service. The transcript of a call never does: the app reads it from this server.

## Upgrading from 0.5.0

1. Back up `data_dir` first: stop the server and copy it, or run `sqlite3 <db> ".backup <file>"`. The database
   migrates to schema 6 on start (step 6: the `push_targets` table for relay keys). Rolling back to 0.5.0 needs
   that backup: 0.5.0 refuses the newer schema, and setting `user_version` back by hand makes the next upgrade fail,
   because step 6 runs again on a database that already has the table.
2. Without `central_account` and `push` nothing else changes.
3. With a 0.5.0 `central_account` (issuer = the account issuer, for example Zitadel) the server does not start until
   you update it: set `issuer` to the wristcall Cloud URL and add `audience` with this server's URL(s). Links made
   with 0.5.0 no longer match: users link again from the app (API token or pairing code).
4. Add `push` to send notifications. Watches only get them in the watch's push build (watch 0.3.0, which needs a paid
   Apple Developer account for a real watch); watches of servers 0.5.0 and older keep polling.

## Upgrading from 0.4.0

1. Back up `data_dir` first: stop the server and copy it, or run `sqlite3 <db> ".backup <file>"`. The database
   migrates to schema 5 on start: a nullable `central_subject` on users and a target on pairing requests (step 4),
   then the call history (step 5: the text of 0.4.0's one-way calls moves to the new `call_entries` table and its
   search index). The YAML stays as it is: nothing is deleted or encrypted until you add a `history` section, and
   nothing changes for pairing until you add `central_account`.
2. The server gains the `PyJWT[crypto]` dependency (included in the image). Replace the old server before starting
   the new one (`stop-first`), as before.
3. Rolling back to 0.4.0 needs the backup taken before the upgrade: 0.4.0 refuses the newer schema, and
   setting `user_version` back by hand makes the next upgrade fail with a duplicate column error, because step 4 runs again on
   a migrated database. Calls made after the upgrade are lost with the restore: keep them with
   `wristcall history export --format json -o calls.json` before rolling back.

## Upgrading from 0.3.0

1. Back up `data_dir` first (as below). The database migrates to version 3 on start (a new
   `calls` table); nothing else changes and the YAML stays as it is. Replace the old server
   before starting the new one (Docker Swarm's default `stop-first`): on start, the server
   marks one-way calls still processing as interrupted, and only one server may use the database.
2. The default `limits.custom_endpoint_types` now includes `webhook`. If your YAML lists the
   types, add `webhook` to let users point one-shot and monologue agents at their own URL.
3. Rolling back to 0.3.0: delete the one-shot and monologue agents first
   (`wristcall agents rm <slug>`; 0.3.0 cannot read an agent without `tts`), stop the
   server, set the schema back with
   `python -c "import sqlite3; sqlite3.connect('<db>').execute('PRAGMA user_version = 2')"`
   (0.3.0 refuses a newer schema) and start the 0.3.0 image. The `calls` table stays and is
   adopted by the next upgrade.

## Upgrading from 0.2.0

1. Back up `data_dir` first: stop the server and copy it, or run `sqlite3 <db> ".backup <file>"`.
2. Start 0.3.0. The database migrates on start; `profiles` become agents of the user `owner`
   once (`default` first) and the existing watches are given to `owner`.
3. Values in old profiles outside the new bounds (`vad`, `timeouts`, empty `fallback_message`...)
   are adjusted on import; the log lists which fields.
4. Keep `profiles` in the YAML while you might roll back (0.2.0 needs `profiles.default`).
   Rolling back is the 0.2.0 image with the same YAML.
5. On a server with several users, give old watches to a user with
   `wristcall devices assign <id> --user <handle>`.
6. Custom endpoints allow only the `openai_*` types by default and make the server call URLs
   your users choose (SSRF): keep `limits.custom_endpoints` on only with trusted users.

## Users and agents

A server has users; each user has watches and agents. An agent is what the watch calls: a
name, an icon (SF Symbol), an STT service (input), an action (the chat model that answers),
a TTS service (output), a language, a prompt and how the turn ends (`auto` by silence, after
`silence_ms`, or `manual` by mute). The watch calls the first agent of its user's list.

Besides `conversation`, an agent can only listen: `one-shot` (say one thing and hang up) or
`monologue` (talk until you hang up). After the call the server transcribes it and posts the
text to the agent's webhook (its action); any `2xx` within about a minute counts as delivered,
after up to three attempts. The client asks `GET /v1/calls/{id}` for the result
([protocol](docs/protocol.md#one-way-calls-one-shot-monologue)).

```bash
wristcall agents add note --name Note --icon note.text --call-type one-shot \
  --action '{"type": "webhook", "url": "https://n8n.example/webhook/notes", "headers": {"Authorization": "Bearer ..."}}'
wristcall-refclient call --agent note --wav note.wav       # hangs up after the WAV and prints the delivery
```

```bash
wristcall users edit owner --handle alice --name Alice     # the user created from `profiles`
wristcall agents add coach --name Coach --icon figure.run --turn-end manual --prompt-file coach.txt
wristcall agents list
wristcall agents edit coach --position 0                   # the watch calls it now
wristcall agents show coach > coach.json                   # edit, then: agents edit coach --from-json coach.json
```

`--stt`, `--action` and `--tts` take a provider name from `wristcall.yaml` (needed only when
the server offers more than one of that kind) or a JSON object with your own service, for
example `--action '{"type": "openai_chat", "base_url": "https://llm.example/v1", "model": "m", "api_key": "..."}'`.
Keys come back as `***`; sending `***` back keeps the stored key.
The same `coach.json` also works with `agents add <new-slug> --from-json coach.json`, which copies the agent.
Custom endpoints make the server request URLs your users choose, including your internal network:
keep `limits.custom_endpoints: true` only when you trust every user of the server.

With more than one user, add `--user <handle>` to `pair`, `devices list|approve|assign`, `agents` and `users tokens add|list`.

## CLI

| Command | Does |
|---|---|
| `wristcall serve` | starts the server (default in the container) |
| `wristcall pair [--user]` | generates an 8 digit code for a user, valid for 10 minutes |
| `wristcall devices list` | lists paired watches (and their user) and pending requests |
| `wristcall devices approve <id> [--user]` | approves a request for a user (requests aimed at another user are not matched) |
| `wristcall devices deny <id>` | denies a pending request: the watch is told on its next check |
| `wristcall devices revoke <id>` | revokes a watch |
| `wristcall devices assign <id> --user <handle>` | gives a watch to a user |
| `wristcall users list\|add\|edit\|rm` | manages users (`list` shows which are linked to the central account) |
| `wristcall users unlink <handle>` | removes a user's link to the central account |
| `wristcall users tokens add\|list\|revoke` | API tokens for the management API |
| `wristcall agents list\|show\|add\|edit\|rm` | manages a user's agents (`--retention` for the history) |
| `wristcall history list\|show\|rm\|clear\|export\|redeliver` | a user's call history |
| `wristcall history new-key\|encrypt\|decrypt` | encryption of the history at rest |

## Management API

With an API token (`wristcall users tokens add`), in `Authorization: Bearer wc_pat_...`:
`GET/POST /v1/agents`, `GET/PATCH/DELETE /v1/agents/{slug or id}`, `GET /v1/providers`,
`GET /v1/devices`, `DELETE /v1/devices/{id}`, `POST /v1/pairing-codes`. With `central_account`:
`POST/DELETE /v1/account/link` and `GET /v1/pairing-requests` with `POST .../{id}/approve|deny`. Call history:
`GET/DELETE /v1/calls`, `GET/DELETE /v1/calls/{id}`, `GET /v1/calls/export`, `POST /v1/calls/{id}/redeliver`. With `push`:
`PUT/DELETE /v1/push` (the app's relay key). The JSON fields are
the ones `wristcall agents show` prints. See [docs/protocol.md](docs/protocol.md#management-api).

## iPhone app

The iPhone app (`watch/Phone/`, tag `ios-vX.Y.Z`) manages your servers from the phone: add a server with its
address and a personal token (`wristcall users tokens add --name iphone`), create, edit and delete agents, approve
or deny watches waiting to pair, make pairing codes, and read, search, export and redeliver the call history of
all servers in one list. It does not make calls; the watch does. The watch app is embedded in the iPhone app, so
"Add watch" can hand a server to the watch over WatchConnectivity.

With the wristcall Cloud in the build, the iPhone also signs in with the central account (Authorization Code
with PKCE), mirrors your servers and agents to the account, and lets the watch sign in with a device code that
you approve on the phone. The default build has no Cloud (`WRISTCALL_CLOUD_URL` empty), so those parts stay
hidden until a Cloud is deployed and set. Push notifications for device approvals, App Store distribution and
Sign in with Apple need a paid Apple Developer account; a free Apple ID builds everything else. There is no
published binary: build it with Xcode, see [watch/README.md](watch/README.md#iphone-app).

Known limitation: the iPhone sign-in could not be verified against the real identity provider, whose hosted
login page was down while this was built. The watch's device code sign-in was verified end to end.

## Documentation

- Client ↔ server protocol: [docs/protocol.md](docs/protocol.md)
- Watch and iPhone apps (build, test, install): [watch/README.md](watch/README.md)
- Pairing directory: [directory/README.md](directory/README.md)
- wristcall Cloud (central account, per-server tokens, push relay): [cloud/README.md](cloud/README.md)
- History: [CHANGELOG.md](CHANGELOG.md)

## License

Apache 2.0. The bundled Silero VAD model is MIT (`server/src/wristcall/vad/SILERO_LICENSE`).
