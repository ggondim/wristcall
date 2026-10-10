# wristcall cloud

The central account API: lets a user sign in with the central account (OIDC, `auth.trigram.com.br`) and keep the
list of their wristcall servers in one place. It also issues the per-server tokens that prove the central account to
a server, and relays push notifications (Web Push and APNs) for servers that have no push credentials of their own.
It is a separate service from `server/`, with its own package (`wristcall-cloud`), its own MongoDB database and its
own release cycle.

## Configuration

Everything comes from the environment:

| Variable | Default | |
|---|---|---|
| `WRISTCALL_CLOUD_MONGO_URL` | (required) | `mongodb://` or `mongodb+srv://` URL |
| `WRISTCALL_CLOUD_DATABASE` | `wristcall_cloud` | |
| `WRISTCALL_CLOUD_ISSUER` | `https://auth.trigram.com.br` | must be https (http only for localhost); trailing slash removed |
| `WRISTCALL_CLOUD_CLIENT_IOS` / `_PWA` / `_WATCH` | | OIDC client ids; at least one is required |
| `WRISTCALL_CLOUD_PROJECT_ID` | | adds the project audience scope to `/v1/config` |
| `WRISTCALL_CLOUD_MAX_SERVERS` | `20` | servers per account |
| `WRISTCALL_CLOUD_MAX_AGENTS_PER_SERVER` | `50` | |
| `PORT` | `8080` | |

Per-server tokens (the first two go together; without them the token routes answer `404 not_configured`):

| Variable | Default | |
|---|---|---|
| `WRISTCALL_CLOUD_PUBLIC_URL` | | the Cloud's own URL, the `iss` of every per-server token. It must already be canonical: https, lowercase host, no default port, no trailing slash (http only for localhost), and differ from `WRISTCALL_CLOUD_ISSUER` |
| `WRISTCALL_CLOUD_SIGNING_KEY` / `_FILE` | | EC P-256 private key (PEM) the tokens are signed with; inline or a file path, not both |
| `WRISTCALL_CLOUD_ALLOW_LOOPBACK_AUDIENCE` | `0` | `1` lets clients ask tokens for `http://localhost` and friends (development only) |

Push relay (all optional; a channel exists only when its credentials are set):

| Variable | Default | |
|---|---|---|
| `WRISTCALL_CLOUD_VAPID_PRIVATE_KEY` / `_FILE` | | EC P-256 private key (PEM) of the Web Push (VAPID) identity; without it there is no `webpush` channel |
| `WRISTCALL_CLOUD_VAPID_SUBJECT` | | `mailto:` or `https://` contact of the operator; required with the key, and an error without it |
| `WRISTCALL_CLOUD_WEBPUSH_HOSTS` | `.push.apple.com,fcm.googleapis.com,.push.services.mozilla.com,.notify.windows.com` | hosts a Web Push endpoint may point to, comma separated; a leading dot means any subdomain, otherwise that host only. It replaces the list, it does not extend it |
| `WRISTCALL_CLOUD_APNS_KEY` / `_FILE` | | the team's `.p8` key (EC P-256, PEM); without it there is no `apns` channel |
| `WRISTCALL_CLOUD_APNS_KEY_ID` | | id of that key |
| `WRISTCALL_CLOUD_APNS_TEAM_ID` | | Apple team id. The key, its id and the team id go together |
| `WRISTCALL_CLOUD_APNS_TOPICS` | | comma separated bundle ids a registration may name; required with the key |
| `WRISTCALL_CLOUD_APNS_URL_PRODUCTION` / `_SANDBOX` | `https://api.push.apple.com` / `https://api.sandbox.push.apple.com` | APNs hosts (https; http only for localhost); only changed to point tests at a fake |
| `WRISTCALL_CLOUD_PUSH_PER_MINUTE` | `30` | sends per push key per minute |
| `WRISTCALL_CLOUD_PUSH_PER_DAY` | `500` | sends per push key per day |
| `WRISTCALL_CLOUD_REGISTRATIONS_PER_MINUTE` | `10` | `POST /v1/push/registrations` per client address per minute |
| `WRISTCALL_CLOUD_CLIENT_IP_HEADER` | | header the reverse proxy puts the client address in (for example `X-Forwarded-For`); its last value counts. Without it, the address of the connection is used |
| `WRISTCALL_CLOUD_PUSH_IDLE_DAYS` | `180` | registrations without a send for this long are deleted (checked every 6 hours) |
| `WRISTCALL_CLOUD_PUSH_FAKE` | `0` | `1` replaces both channels by one that delivers nothing, for end to end tests on one machine. It is refused unless `WRISTCALL_CLOUD_PUBLIC_URL` is a loopback URL, and when VAPID or APNs credentials are set as well, the fake channel is the one used |

Configuration errors name the variable, never its value, and stop the start with exit code 2: a VAPID subject without
a key, a VAPID key without a subject, one or two of the three APNs credentials, and a bad key, URL or number are all
errors.

The `httpx`, `httpcore`, `pymongo`, `h2` and `hpack` loggers are kept at WARNING: at INFO or DEBUG they print request
URLs, HTTP/2 headers and database documents, which hold device tokens, Web Push endpoints and keys. Do not lower
them in a deployment.

### Generating keys

Both keys are EC P-256 private keys in PEM (PKCS#8). They are generated once and kept as secrets (a Docker secret,
passed with the `_FILE` variables):

```sh
openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt -out cloud-signing.pem
openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt -out vapid.pem
```

`cloud-signing.pem` is `WRISTCALL_CLOUD_SIGNING_KEY_FILE`, `vapid.pem` is `WRISTCALL_CLOUD_VAPID_PRIVATE_KEY_FILE`.
The APNs key is the `.p8` file from the Apple developer account, used as it is.

## Authentication

Requests carry the central account access token: `Authorization: Bearer <token>`. The token is verified locally
against the issuer's published keys (signature, issuer, time claims); its audience and the client it was issued
to must be one of the configured client ids. The account is identified as `<issuer>#<subject>`. A missing or bad
token is `401 unauthorized`; the issuer being unreachable with no cached keys is `503 account_unavailable`.

**Security: the Zitadel token is not bound to this API.** The Cloud accepts the same watch, iOS and PWA client ids
that wristcall servers 0.5.0 accept. A central access token given to a server operator (when the user pairs or links
there) can therefore be replayed here by that operator until it expires: a malicious self-hosted operator could read
the user's agenda and add or delete servers (for example a phishing "Home" entry). Only `DELETE /v1/account` is
restricted (iOS and PWA clients). Servers 0.6.0 never receive the Zitadel token, only a per-server token (below),
which the Cloud API does not accept: see "Before deploying".

`src/wristcall_cloud/oidc.py` is a copy of `server/src/wristcall/oidc.py` (only the user agent differs); keep
both in sync.

## API

Errors are always `{"error": code, "message": ...}`. Request bodies are limited to 64 KiB (`413 too_large`); a body
that is not a JSON object is `422 invalid`.

- `GET /v1/health`: `{"status": "ok", "version": ...}`.
- `GET /v1/config` (public): what an app needs to sign in: `issuer`, `project_id`, `clients` and `scopes`.

All the routes below need the account token. The account is created on first use. A resource of another account
answers `404 not_found`, never `403`.

| Route | Body | Answers |
|---|---|---|
| `GET /v1/account` | | `200 {"account", "created_at", "servers"}` |
| `DELETE /v1/account` | | `204`, deletes the account and its servers; `403 forbidden` unless the token was issued to the iOS or PWA client |
| `GET /v1/servers` | | `200 {"servers": [Server]}` by creation |
| `POST /v1/servers` | `{name, url, kind?, linked?}` | `201 Server`; `409 conflict` for a repeated URL; `403 limit` over `WRISTCALL_CLOUD_MAX_SERVERS` |
| `GET /v1/servers/{id}` | | `200 Server`; `404` |
| `PATCH /v1/servers/{id}` | `{name?, linked?}` | `200 Server`; `404`; `422` (the URL and the kind cannot change: delete and create) |
| `DELETE /v1/servers/{id}` | | `204`; `404` |
| `PUT /v1/servers/{id}/agents` | `{"agents": [Agent]}` | `200 {"agents"}` (replaces the list); `403 limit` over `WRISTCALL_CLOUD_MAX_AGENTS_PER_SERVER`; `404`; `422` |
| `GET /v1/agents` | | `200 {"agents": [Agent + server_id, server_name, server_url]}` in the order of the servers and of each list |

`Server` is `{id, name, url, kind, linked, agents, created_at, updated_at}`. `name` is 1 to 64 characters without
control characters. `url` is http or https with a host, without user, password, query or fragment (up to 2048
characters); it is normalized (scheme and host in lowercase, no trailing slash) and unique per account. `kind` is
`self-hosted` (default) or `cloud`; `linked` is a boolean (default `false`).

`Agent` is `{id, slug, display_name, icon, call_type}`: `id` and `slug` are 1 to 64 characters of `A-Za-z0-9_-`,
`display_name` 1 to 64 characters, `icon` an SF Symbol name (1 to 64 characters of `a-z0-9.`), `call_type` one of
`conversation`, `one-shot`, `monologue`. Ids must be unique in the list and unknown fields are `422 invalid`.

### Per-server tokens

The Cloud is an OIDC issuer of its own (`WRISTCALL_CLOUD_PUBLIC_URL`) whose tokens are meant for one server. An app
asks for a token for the exact URL it is about to connect to, with its central account token, and gives that token
to that server only. The server checks signature, issuer, `aud` (its own URL), `typ` and expiry; the Cloud API
refuses these tokens, and servers refuse Zitadel tokens, so a token cannot be replayed at another server or at the
Cloud. Tokens are ES256, `typ` `wc-server+jwt`, valid for 300 seconds, with `sub` the account (`<issuer>#<subject>`)
and `client_id` the app the account token was issued to. Neither the audience nor the token reach the log.

| Route | Auth | Answers |
|---|---|---|
| `GET /.well-known/openid-configuration` | none | `200 {"issuer", "jwks_uri", "id_token_signing_alg_values_supported"}`; `404 not_configured` without a signing key |
| `GET /v1/jwks` | none | `200 {"keys": [JWK]}` with `Cache-Control: public, max-age=3600`; `404 not_configured` |
| `POST /v1/server-tokens` | account token | body `{"audience": url}`; `200 {"token", "audience", "expires_at"}` with the audience in its canonical form. `422 invalid` for a malformed audience, a loopback one (unless `WRISTCALL_CLOUD_ALLOW_LOOPBACK_AUDIENCE`), or the Cloud's or the issuer's own URL; `404 not_configured` |

`GET /v1/config` also tells apps whether the Cloud issues server tokens (`server_tokens`).

### Push relay

Servers that cannot reach Apple or the browsers' push services themselves send notifications through the Cloud.
A device (an iPhone, a PWA, the watch) registers anonymously and receives a push key, `wc_push_` followed by 43
characters. The Cloud stores only the SHA-256 of the key, so a copy of the database cannot send anything. The device
gives the key to the server it wants notifications from; the server sends with it. Matrix push gateways work the
same way.

The label and the tag of a registration are forced on every message sent with its key (the label is the
notification's subtitle, the tag goes into `wristcall.tag` of the payload), so a leaked key cannot pass for another
server. A registration lists the `events` it accepts, a subset of `call.finished` and `device.approval`; `test` is
always accepted, any other event is `422 invalid`. Up to 20 registrations exist per APNs token or Web Push
endpoint (the oldest goes). Registrations without a send for `WRISTCALL_CLOUD_PUSH_IDLE_DAYS` are deleted.

| Route | Auth | Answers |
|---|---|---|
| `POST /v1/push/registrations` | none; limited per client address | `201 {"push_key", "platform", "label", "tag", "events"}`; `404 not_configured` when the Cloud has no channel for the platform; `422 invalid`; `429 rate_limited` with `Retry-After` |
| `GET /v1/push/registrations/current` | push key | `200 {"platform", "label", "tag", "events"}`; `410 gone` when the key is unknown |
| `DELETE /v1/push/registrations/current` | push key | `204`; `404 not_found` |
| `POST /v1/push/send` | push key | `202 {"status": "sent"}`; `410 gone` (unknown key, or the push service says the device is gone: the registration is deleted); `422 invalid`; `429 rate_limited` with `Retry-After`; `503 push_unavailable` |

`Authorization: Bearer wc_push_...` carries the key; a missing one is `401 unauthorized`. A database error is always
`503 push_unavailable`, never `410`, because `410` tells a server to forget the key for good.

Registration bodies:

- APNs: `{"platform": "apns", "token": <hex device token>, "topic": <one of WRISTCALL_CLOUD_APNS_TOPICS>, "environment": "sandbox" | "production", "label", "tag"?, "events"}`.
- Web Push: `{"platform": "webpush", "subscription": <PushSubscription.toJSON()>, "label", "tag"?, "events"}`. The endpoint must be https, port 443, on a host of `WRISTCALL_CLOUD_WEBPUSH_HOSTS` (anything else is `422` and nothing is requested); redirects are never followed. `label` is 1 to 64 characters, `tag` up to 64 characters of `A-Za-z0-9_.:-`.

Send body: `{"event", "title", "body"?, "data"?, "ttl_s"?, "collapse_id"?}`. `title` is 1 to 100 characters, `body` up to
300, `data` an object of up to 1024 bytes of JSON, `ttl_s` 0 to 86400 (default 3600), `collapse_id` 1 to 64
characters of `A-Za-z0-9_.:-`. Unknown fields are `422`. Web Push messages are encrypted for the browser (RFC 8291,
`aes128gcm`) and signed with VAPID (RFC 8292); APNs messages use HTTP/2 and a provider token that is renewed every 30
minutes, or when Apple refuses it.

`GET /v1/config` reports `push`: `apns` and `webpush` (whether the Cloud has the channel), `vapid_public_key` (what a
browser needs to subscribe) and `apns_topics` (empty when the APNs channel is not configured).

**Privacy.** The notification title, body and label pass through the Cloud and then through Apple or the browser's
push service (a Web Push payload is encrypted for the browser, but the push service still sees that a message was sent; Apple
sees the alert of an APNs message). The transcript of a call never does: only what a server puts in the notification is sent. The Cloud does not
log titles, bodies, labels, keys, device tokens or endpoints.

## Rate limiting

Account routes: the service does not limit request rates itself. The reverse proxy of the deployment (Traefik) is
expected to, per client address, in front of every route.

The push relay limits itself, in memory (one replica; the counters restart with the process, which is accepted):
`WRISTCALL_CLOUD_PUSH_PER_MINUTE` and `_PER_DAY` per push key, and `WRISTCALL_CLOUD_REGISTRATIONS_PER_MINUTE` per
client address on the anonymous registration. Set `WRISTCALL_CLOUD_CLIENT_IP_HEADER` behind a proxy, or every client
shares the proxy's address.

## Before deploying

- **Replay between servers.** The block that kept the Cloud API from being deployed (a Zitadel token given to a server
  could be replayed here) is lifted once every server linked to a Cloud account runs 0.6.0 with
  `central_account.audience` set: those servers take only per-server tokens. A server still on 0.5.0 with an
  `issuer` of Zitadel keeps the problem for its own operator, so upgrade (or unlink) them first.
- **Registration limit at the proxy.** `POST /v1/push/registrations` is anonymous. Besides the Cloud's own limit per
  client address, put a limit on it in Traefik, and set `WRISTCALL_CLOUD_CLIENT_IP_HEADER` to the header Traefik
  writes.
- **Keys do not change without a plan.** Changing the VAPID key invalidates every Web Push subscription (each device
  must subscribe again). Changing the signing key makes the per-server tokens already issued fail for up to one
  minute, until servers fetch the new key. The APNs key can be replaced by a new one of the same team and topics.
- **Store the keys as secrets** (`_FILE` variables), and never run with `WRISTCALL_CLOUD_PUSH_FAKE` outside a local test.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
WRISTCALL_CLOUD_TEST_MONGO_URL=mongodb://localhost:27017 .venv/bin/python -m pytest -q
docker build -t wristcall-cloud .
```

Tests need a MongoDB (`WRISTCALL_CLOUD_TEST_MONGO_URL`, default `mongodb://localhost:27017`); each test uses a
throwaway `test_<uuid>` database and drops it.
