# wristcall cloud

The central account API: lets a user sign in with the central account (OIDC, `auth.trigram.com.br`) and keep the
list of their wristcall servers in one place. It also issues the per-server tokens that prove the central account to
a server, and relays push notifications (Web Push and APNs) for servers that have no push credentials of their own.
Since 0.4.0 the Cloud also owns the account itself: apps create a silent account from a key made on the device, and
each device gets its own session (see [Accounts and sessions](#accounts-and-sessions)). Since 0.5.0 it also registers
the nodes of cloud mode and serves them an internal API (see [Cloud nodes](#cloud-nodes)). It is a separate service from
`server/`, with its own package (`wristcall-cloud`), its own MongoDB database and its own release cycle.

## Configuration

Everything comes from the environment:

| Variable | Default | |
|---|---|---|
| `WRISTCALL_CLOUD_MONGO_URL` / `_FILE` | (required) | `mongodb://` or `mongodb+srv://` URL (it carries the password), inline or the path of a file that holds it, not both. The admin CLI reads the same variables |
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

Accounts and sessions (all optional; they need the two per-server token variables above, because the same key signs
the access tokens, and without them `sessions.enabled` in `/v1/config` is `false`):

| Variable | Default | |
|---|---|---|
| `WRISTCALL_CLOUD_LEGACY_ZITADEL_TOKENS` | `0` | `1` keeps Zitadel tokens working on the routes that existed in 0.3.x. See [Transition flag](#transition-flag) |
| `WRISTCALL_CLOUD_ZITADEL_MAX_AGE` | `600` | seconds; the oldest a Zitadel token may be (`now - iat`) when it is exchanged at `POST /v1/devices` |
| `WRISTCALL_CLOUD_ACCESS_TOKEN_TTL` | `3600` | seconds an access token lives |
| `WRISTCALL_CLOUD_ACCOUNTS_PER_IP_HOUR` | `3` | silent accounts one client address may create per hour |
| `WRISTCALL_CLOUD_ACCOUNTS_PER_IP_DAY` | `10` | the same, per day |
| `WRISTCALL_CLOUD_ACCOUNTS_PER_DAY` | `200` | silent accounts created in the last 24 hours, all clients together; above it `POST /v1/accounts` answers `429` |
| `WRISTCALL_CLOUD_ACCOUNTS_ALERT_PER_DAY` | `100` | a warning is logged above this many in 24 hours; must not exceed `WRISTCALL_CLOUD_ACCOUNTS_PER_DAY` |
| `WRISTCALL_CLOUD_SESSION_ATTEMPTS_PER_MINUTE` | `20` | per client address, on the routes that take a secret to guess: account key, enroll code, web session creation, invite code |
| `WRISTCALL_CLOUD_MAX_DEVICES` | `10` | devices per account |
| `WRISTCALL_CLOUD_MAX_ENROLL_CODES` | `5` | open enroll codes per account |
| `WRISTCALL_CLOUD_ACCOUNT_IDLE_DAYS` | `90` | an anonymous account (no identity, no plan) unused for this long is deleted (checked every 6 hours); a device unused for this long cannot refresh its session |
| `WRISTCALL_CLOUD_CLIENT_COUNTRY_HEADER` | | header the reverse proxy puts the client's country in (`CF-IPCountry` behind Cloudflare). It is recorded on web sessions, so the iPhone can show it before approving, and (since 0.7.0) on the entitlement an invite creates. Without it the country is `null`; nothing breaks |

Cloud nodes (all optional; the node routes need the two per-server token variables above, and answer
`404 not_configured` without them, except `POST /v1/node/heartbeat`). A value that is not a positive whole number stops the start, naming the variable:

| Variable | Default | |
|---|---|---|
| `WRISTCALL_CLOUD_QUOTA_MINUTES` | `600` | minutes of calls per account per calendar month (UTC), the same for every plan and for all call types together |
| `WRISTCALL_CLOUD_MAX_CONVERSATION_S` | `3600` | longest conversation, in seconds |
| `WRISTCALL_CLOUD_MAX_ONE_WAY_S` | `1800` | longest one-shot or monologue call, in seconds |
| `WRISTCALL_CLOUD_NODE_REQUESTS_PER_MINUTE` | `1200` | requests per node per minute on `/v1/node/*` (counted in memory, per node id) |
| `WRISTCALL_CLOUD_MAX_CLOUD_AGENTS` | `20` | cloud-mode agents per account |
| `WRISTCALL_CLOUD_HISTORY_RETENTION_DAYS` | `90` | how long a call is kept when its agent says nothing |
| `WRISTCALL_CLOUD_MAX_HISTORY_RETENTION_DAYS` | `365` | the ceiling of an agent's `retention_days`, and what `"forever"` means; the default above must not exceed it |

Three more values are not configurable: a node counts as silent after 120 seconds without a heartbeat, the sweeper
runs every 30 seconds, and a call younger than 30 seconds is never closed as an orphan.

Routes and the Cloud entry (all optional; see [Routes and the Cloud entry](#routes-and-the-cloud-entry)). A value that
does not parse stops the start, naming the variable:

| Variable | Default | |
|---|---|---|
| `WRISTCALL_CLOUD_ROUTE_REGIONS` | `BR=sao,gru;US=iad;CA=iad;MX=iad;*=iad` | country to regions, best first: `COUNTRY=region,region;...`, a country is two capital letters or `*`, a region three lowercase letters. The `*` row is required and serves every other country (and a missing one) |
| `WRISTCALL_CLOUD_CLOUD_ENTRY_CLIENTS` | | minimum app version that sees the Cloud entry, per platform. Recommended: `ios=0.3.0,watchos=0.6.0,web=0.4.0`, the first app versions that handle the Cloud entry (platforms `ios`, `ipados`, `macos`, `watchos`, `web`, each once). Empty: nobody sees it |
| `WRISTCALL_CLOUD_VOICE_NOTES_LANGUAGE` | `en` | language of the "Voice notes" agent for an account whose country is not Portuguese-speaking (a code such as `en` or `pt-BR`) |
| `WRISTCALL_CLOUD_ALERT_URL` (or `_FILE`) | | since 0.7.0, the [ntfy](https://ntfy.sh) topic URL that hears of an always-on node gone silent. A secret (the topic name is the password): never logged. Must be `https`. Without it there are no alerts and no loop. See [Node alert](#node-alert) |
| `WRISTCALL_CLOUD_ALERT_AFTER_S` | `900` | seconds without a heartbeat before an always-on node raises the alert |
| `WRISTCALL_CLOUD_VOICE_NOTES_STT` | `stt-default` | name of the operator's speech to text provider on the nodes, written in "Voice notes" |

Five more values are not configurable: an always-on node is fresh for 45 seconds after its last heartbeat (and for 60
seconds after the Cloud starts, see below), failure reports of a node count for 300 seconds and block it from 3 accounts,
and `503 no_node_available` asks to wait 30 seconds.

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
| `WRISTCALL_CLOUD_CORS_ORIGINS` | | comma separated browser origins allowed to call the API (the wristcall PWA's, for example `https://wristcall-app.trigram.com.br`). Exact `scheme://host[:port]`: `https`, or `http` only for `localhost`, `127.0.0.1` and `[::1]`; no path, trailing slash, `*` or user. Empty: no CORS, as before. See [Browsers (CORS)](#browsers-cors) |

Account deletion at the identity provider (the three go together; without them `DELETE /v1/account` deletes only the
agenda):

| Variable | Default | |
|---|---|---|
| `WRISTCALL_CLOUD_ZITADEL_ORG_ID` | | Zitadel organization whose users the Cloud may delete (wristcall's: `394311486572333063`); also adds its org scope to `/v1/config`, so the login shows the org's branding |
| `WRISTCALL_CLOUD_ZITADEL_CLIENT_ID` | | client id of the Cloud's machine user there (`wristcall-cloud`), which needs the organization role `ORG_USER_MANAGER` and nothing else |
| `WRISTCALL_CLOUD_ZITADEL_CLIENT_SECRET` / `_FILE` | | that machine user's client secret |

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

Requests carry a Cloud account access token: `Authorization: Bearer <token>`. A missing or bad token is
`401 unauthorized`.

**Account access token.** The Cloud signs it (ES256, header `typ` `wc-account+jwt`, with the key of the per-server
tokens; `iss` and `aud` are `WRISTCALL_CLOUD_PUBLIC_URL`). Claims: `sub` the account id, `did` the device id, `plt`
the device platform. It lives 3600 seconds (`WRISTCALL_CLOUD_ACCESS_TOKEN_TTL`) and works on every account route.
Every request checks that the device still exists, so a device that was removed or revoked stops at once, not when
the token expires. Servers do not accept it (they require `typ` `wc-server+jwt` and their own URL as `aud`), and the
Cloud refuses to issue a per-server token for its own URL.

**Zitadel token.** The central account access token from the identity provider. It is verified locally against the
issuer's published keys (signature, issuer, time claims); its audience must be one of the configured client ids or the
project id (`WRISTCALL_CLOUD_PROJECT_ID`, which Zitadel puts in `aud` when the app asks for the project audience scope),
and the client it was issued to must be one of the configured client ids. It is taken in two places only:

- `POST /v1/devices`, to exchange it for a device session, and only if it was issued at most
  `WRISTCALL_CLOUD_ZITADEL_MAX_AGE` seconds ago (`401 stale_token` otherwise: the app signs in again).
- The routes that already existed in 0.3.x, and only while the [transition flag](#transition-flag) is on.

The identity provider being unreachable with no cached keys is `503 account_unavailable`. Without a signing key the
Cloud issues no sessions, so without the transition flag no route accepts a token at all.

**Security: the Zitadel token is not bound to this API.** The Cloud accepts the same watch, iOS and PWA client ids
that wristcall servers 0.5.0 accept. A central access token given to a server operator (when the user pairs or links
there) can therefore be replayed here by that operator until it expires: a malicious self-hosted operator could read
the user's agenda and add or delete servers (for example a phishing "Home" entry) while the transition flag is on, and,
for the first `WRISTCALL_CLOUD_ZITADEL_MAX_AGE` seconds, exchange the token for a device of the user's account
(`POST /v1/devices`). Servers 0.6.0 never receive the Zitadel token, only a per-server token (below), which the Cloud
API does not accept: see "Before deploying".

**Account deletion.** With the `WRISTCALL_CLOUD_ZITADEL_*` variables, `DELETE /v1/account` also deletes the users
behind the account's Zitadel identities at Zitadel (`DELETE /management/v1/users/{id}` with `x-zitadel-orgid`), signed
in as the machine user (client credentials, token cached until a minute before it expires). The call is scoped to the
organization: a user of another organization of the instance (who may sign in to the apps too) is not found there and
is left alone, as is one already deleted (a warning is logged: for every user it means a wrong organization id). After
the deletion Zitadel refuses the user's refresh token, and its end-session page sends the app straight back. Devices,
identities, codes, web sessions and the plan go with the account, so every account access token stops working at once.
Known gaps: the app's confirmation says the sign-in is deleted even when the Cloud lacks these variables or the user
belongs to another organization; and, while the transition flag is on, a Zitadel token issued before the deletion
stays valid until it expires, so a call with it in that window records an empty account again, which nothing
deletes later.

`src/wristcall_cloud/oidc.py` is a copy of `server/src/wristcall/oidc.py` (only the user agent differs); keep
both in sync.

## API

Errors are always `{"error": code, "message": ...}`. Request bodies are limited to 64 KiB (`413 too_large`), and to 1 MiB under `/v1/node/`; a body
that is not a JSON object is `422 invalid`.

- `GET /v1/health`: `{"status": "ok", "version": ...}`.
- `GET /v1/config` (public): what an app needs to sign in: `issuer`, `project_id`, `clients` and `scopes`; the
  `push` and `server_tokens` fields below; and `sessions`: `{"enabled", "zitadel_max_age_s", "legacy_zitadel_tokens"}`
  (`enabled` is whether the Cloud issues account sessions).

All the routes below need an account token (an access token; a Zitadel token only while the transition flag is on,
and then the account is created on first use). A resource of another account answers `404 not_found`, never `403`.

| Route | Body | Answers |
|---|---|---|
| `GET /v1/account` | | `200 {"account", "created_at", "servers", "plan", "identities", "key_set"}`: `plan` is `{"plan", "source", "expires_at"}` or `null`, `identities` the kinds linked (`["zitadel"]`), `key_set` whether the account has an account key |
| `DELETE /v1/account` | | `204`, deletes the account, its servers, its cloud-mode data (agents, calls, history, push targets and usage), devices, identities and plan, and (when configured) the Zitadel users behind it; `403 forbidden` unless the device is an app or the web app (for a Zitadel token: unless it was issued to the iOS or PWA client); `503 account_unavailable` when the provider fails (the agenda is already gone; the same call again finishes) |
| `GET /v1/servers` | | `200 {"servers": [Server]}` by creation |
| `POST /v1/servers` | `{name, url, kind?, linked?}` | `201 Server`; `409 conflict` for a repeated URL; `403 limit` over `WRISTCALL_CLOUD_MAX_SERVERS` (the Cloud entry does not count); `422 invalid` for `kind: "cloud"` (only the Cloud creates that entry, see [Routes and the Cloud entry](#routes-and-the-cloud-entry); `kind: "cloud"` exists only on the Cloud-managed entry) |
| `GET /v1/servers/{id}` | | `200 Server`; `404` |
| `PATCH /v1/servers/{id}` | `{name?, linked?}` | `200 Server`; `404`; `409 managed_by_cloud` (`404` to clients that cannot see the Cloud entry); `422` (the URL and the kind cannot change: delete and create) |
| `DELETE /v1/servers/{id}` | | `204`; `404`; `409 managed_by_cloud` (`404` to clients that cannot see the Cloud entry) |
| `PUT /v1/servers/{id}/agents` | `{"agents": [Agent]}` | `200 {"agents"}` (replaces the list); `403 limit` over `WRISTCALL_CLOUD_MAX_AGENTS_PER_SERVER`; `404`; `409 managed_by_cloud` (`404` to clients that cannot see the Cloud entry); `422` |
| `GET /v1/agents` | | `200 {"agents": [Agent + server_id, server_name, server_url]}` in the order of the servers and of each list |

`Server` is `{id, name, url, kind, linked, agents, created_at, updated_at}`. `name` is 1 to 64 characters without
control characters. `url` is http or https with a host, without user, password, query or fragment (up to 2048
characters); it is normalized (scheme and host in lowercase, no trailing slash) and unique per account. `kind` is
`self-hosted` (default) or `cloud`; `linked` is a boolean (default `false`).

`Agent` is `{id, slug, display_name, icon, call_type, color?}`: `id` and `slug` are 1 to 64 characters of `A-Za-z0-9_-`,
`display_name` 1 to 64 characters, `icon` an SF Symbol name (1 to 64 characters of `a-z0-9.`), `call_type` one of
`conversation`, `one-shot`, `monologue`. `color` is optional: one of `red`, `orange`, `yellow`, `green`, `mint`,
`teal`, `blue`, `indigo`, `purple`, `pink`, or `null` (accepted and dropped). It is returned only when stored, so an
agent without a color has no `color` key. Ids must be unique in the list and unknown fields are `422 invalid`.

### Per-server tokens

The Cloud is an OIDC issuer of its own (`WRISTCALL_CLOUD_PUBLIC_URL`) whose tokens are meant for one server. An app
asks for a token for the exact URL it is about to connect to, with its account token, and gives that token to that
server only. The server checks signature, issuer, `aud` (its own URL), `typ` and expiry; the Cloud API refuses
these tokens, and servers refuse Zitadel tokens, so a token cannot be replayed at another server or at the Cloud.
Tokens are ES256, `typ` `wc-server+jwt`, valid for 300 seconds, with `sub` the account id (`acc_...` for accounts made
by 0.4.0, `<issuer>#<subject>` for older ones; opaque to servers) and `client_id` the OIDC client of the app that
asked. With an account session the client comes from the device platform (`ios`, `ipados` and `macos`: the iOS client;
`watchos`: the watch client; `web`: the PWA client), left out when that client is not configured; with a Zitadel token
it is the client the token was issued to. Neither the audience nor the token reach the log.

| Route | Auth | Answers |
|---|---|---|
| `GET /.well-known/openid-configuration` | none | `200 {"issuer", "jwks_uri", "id_token_signing_alg_values_supported"}`; `404 not_configured` without a signing key |
| `GET /v1/jwks` | none | `200 {"keys": [JWK]}` with `Cache-Control: public, max-age=3600`; `404 not_configured` |
| `POST /v1/server-tokens` | account token | body `{"audience": url}`; `200 {"token", "audience", "expires_at"}` with the audience in its canonical form. `422 invalid` for a malformed audience, a loopback one (unless `WRISTCALL_CLOUD_ALLOW_LOOPBACK_AUDIENCE`), the Cloud's or the issuer's own URL, or the URL of a registered node (a node takes the [user token](#authentication-of-a-node) the node routes ask for, not this one; disabled nodes count); `404 not_configured` |

`GET /v1/config` also tells apps whether the Cloud issues server tokens (`server_tokens`).

### Push relay

Servers that cannot reach Apple or the browsers' push services themselves send notifications through the Cloud.
A device (an iPhone, a PWA, the watch) registers anonymously and receives a push key, `wc_push_` followed by 43
characters. The Cloud stores only the SHA-256 of the key, so a copy of the database cannot send anything. The device
gives the key to the server it wants notifications from; the server sends with it. Matrix push gateways work the
same way.

The label and the tag of a registration are forced on every message sent with its key (the label is the
notification's subtitle, the tag goes into `wristcall.tag` of the payload), so a leaked key cannot pass for another
server. A registration lists the `events` it accepts, a subset of `call.finished`, `device.approval` and `quota.warning`; `test` is
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

## Accounts and sessions

Since 0.4.0 the Cloud owns the account. An app creates a silent account from a key it makes on the device, with no
sign-in, and every device then has a session of its own. Signing in with Zitadel is optional and adds an identity to
the account. All the routes here answer `404 not_configured` when the Cloud has no signing key.

### Model

- **Account.** New accounts have an id `acc_` plus 26 lowercase base32 characters (128 bits). Accounts that existed in
  0.3.x keep their id (`<issuer>#<subject>`) and get a Zitadel identity from the [migration](#data-and-migration).
- **Account key.** 32 random bytes made by the app, sent as base64url without padding (43 characters, in its canonical
  form). The Cloud keeps only its SHA-256. It is the proof of ownership for a silent account: the iPhone, iPad and
  Mac keep it in iCloud Keychain, so a new Apple device signs back in with it (`POST /v1/devices`). One key per
  account, and a key belongs to one account.
- **Device.** One session per device, with a platform: `ios`, `ipados`, `macos` (the apps), `watchos` or `web` (the
  PWA). A session is an access token (1 hour) and a rotating refresh token. A refresh token can be used once: using
  it again (a lost response that was resent, or a stolen copy) signs the device out at once, and it must sign in
  again.
- **Identity.** A Zitadel user linked to an account, unique by `(kind, issuer, subject)`. An identity belongs to one
  account and accounts are never merged.
- **Plan.** A beta entitlement, `full` or `custom`, one per account. See [Admin CLI](#admin-cli).

### Secret formats

Secrets are stored only as SHA-256 digests, and never reach a log, an error response or an exception message.

| Secret | Format |
|---|---|
| account key | 43 characters, base64url of 32 bytes, no padding |
| access token | ES256 JWT, `typ` `wc-account+jwt`, claims `iss` and `aud` (`WRISTCALL_CLOUD_PUBLIC_URL`), `sub`, `did`, `plt`, `iat`, `exp`, `jti`; not stored |
| refresh token | `wcr_` plus 43 characters |
| enroll code | `wce_` plus 26 lowercase base32 characters; single use, 600 seconds |
| web session | id `ws_` plus 16 hex characters; approve key `wck_` plus 43 characters; poll key `wcp_` plus 43 characters; 300 seconds |
| invite code | `WC-XXXX-XXXX-XXXX`, 12 uppercase base32 characters; case, spaces and hyphens do not count when redeeming; single use |

### Platform rules

- **App** is `ios`, `ipados` or `macos`. **Manager** is an app or `web`.
- Only an app can set or rotate the account key, create enroll codes, and inspect or approve a web session.
- Only a manager can delete the account, remove another device or redeem an invite. Any device can list the devices,
  remove itself and ask for per-server tokens.
- The watch (`watchos`) is the least privileged device. It cannot do any of the above, but it can still read and
  change the agenda (servers and agents) like in 0.3.x.
- A Zitadel token can be exchanged only for a device that fits its client: the iOS client for `ios`, `ipados` or
  `macos`, the watch client for `watchos`, the PWA client for `web` (`403 forbidden` otherwise). On the 0.3.x routes a
  Zitadel token from a client the Cloud does not know counts as the watch.

### Node alert

Since 0.7.0 the Cloud can tell you when an always-on node stops sending heartbeats, like a night watchman who rings
the bell only in working hours. Set `WRISTCALL_CLOUD_ALERT_URL` to an ntfy topic URL (`https` only; the topic name is
the password, so give it as a secret, or with `_FILE`). Without it nothing runs.

Once a minute the Cloud looks at the registered nodes. A node raises the alert when it is enabled, `always_on`, and has
sent no heartbeat for more than `WRISTCALL_CLOUD_ALERT_AFTER_S` (default 900; a node never heard from counts from the
Cloud's own start), between 07:00 and 23:00 in Brasília, while at least one account has a plan in force (the same
filter as `admin stats`). Nothing is sent for `on_demand` or disabled nodes. The message is a plain text POST with
the ntfy headers `Title: wristcall-node`, `Priority: high` and `Tags: warning`:

```
wristcall node home: no heartbeat for 15 min
```

When the node beats again (at any hour) one more message goes out, `Priority: default`, `Tags: white_check_mark`:
`wristcall node home: heartbeat back`. One message per outage, not one per minute. A node counts as down only after
ntfy answered `2xx`; a refused or lost message is sent again by the next round. The set of nodes known to be down is
kept in memory (one Cloud replica): after a restart a node that is still silent is announced once more. Failures are
logged by exception type or status only, never the URL.

## Routes

A device in a request body is `{"name": 1 to 64 characters, with no control or format characters (newlines, bidi
overrides, zero-width characters: `422 invalid`), "platform", "public_key"?: up to 1024 characters,
"attestation"?: accepted and ignored}`; other fields are ignored. A session answer (`201` or `200`) is
`{"account", "device", "access_token", "refresh_token", "expires_at"}` (`expires_at` is the access token's, in Unix
seconds); `POST /v1/devices` adds `"key_set"` (whether the account has an account key).

"Account" in the Auth column is `Authorization: Bearer <access_token>`; "app" and "manager" are the platform rules
above.

| Route | Auth | Body and answer | Errors |
|---|---|---|---|
| `POST /v1/accounts` | none | `{account_key, device}` creates a silent account and its first device: `201` session | `409 key_in_use`, `422 invalid`, `429 rate_limited` (with `Retry-After`) |
| `POST /v1/devices` | none (the key) or `Bearer <Zitadel token>` | `{account_key, account?, device}` or `{device}`: `201` session and `key_set`. With the key alone the Cloud finds the account (a repeated `POST /v1/accounts` is recovered this way); `account`, if given, must match. With a Zitadel token it finds or creates the account of that identity. The app asks Zitadel for a new token before the exchange | `401 unauthorized`, `401 stale_token`, `403 forbidden` (client does not fit the platform), `403 limit`, `422 invalid`, `429`, `503 account_unavailable` |
| `POST /v1/session/refresh` | none | `{refresh_token, assertion?}`: `200` session with a new refresh token (`assertion` is ignored) | `401 unauthorized` (unknown, expired or idle), `401 revoked` (reuse: the device is gone) |
| `GET /v1/devices` | account | `200 {"devices": [{id, name, platform, created_at, last_seen_at, current}]}` | |
| `DELETE /v1/devices/{id}` | account (manager, or any device for its own id) | `204` | `403 forbidden`, `404 not_found` |
| `PUT /v1/account/key` | account (app) | `{account_key}`: `204`; sets the key when there is none, the same key again is `204` | `409 key_exists`, `409 key_in_use`, `422` |
| `POST /v1/account/key/rotate` | account (app) | `{new_account_key}` plus exactly one proof: `account_key` (the current key) or `zitadel_token` (a fresh Zitadel access token of the iOS app, for a user of this account). `200 {"revoked": n}`. Every other device, every open enroll code and every approved web session of the account goes. With `zitadel_token` the new key replaces the current one whether or not there is one (a reset) | `401 unauthorized` (wrong current key, or a bad sign-in token), `401 stale_token` (sign-in token older than `WRISTCALL_CLOUD_ZITADEL_MAX_AGE`), `403 forbidden` (the sign-in is of another account, or not of the iOS app), `409 key_in_use`, `409 no_key` (with `account_key` only; use `PUT /v1/account/key`), `422 invalid` (both proofs or neither), `429`, `503 account_unavailable` |
| `POST /v1/devices/enroll-codes` | account (app) | `201 {"code", "expires_at"}`: single use, 600 seconds, creates only a `watchos` device | `403 limit` (5 open) |
| `POST /v1/devices/enroll` | none | `{code, device}`: `201` session | `404 invalid_code`, `403 forbidden` (platform is not `watchos`; the code is not spent), `403 limit` (the code is spent), `422`, `429` |
| `POST /v1/web-sessions` | none | `{device: {name, platform?: "web"}}`: `201 {"id", "approve_key", "poll_key", "expires_at"}` (300 seconds) | `422`, `429` |
| `GET /v1/web-sessions/{id}` | `Bearer <poll_key>` | `200 {"status": "pending", "expires_at"}`, or `200 {"status": "approved", ...session}` once (then it is gone). Poll every 2 seconds or more | `401 unauthorized` (no bearer), `404 not_found` (unknown, expired or wrong key), `403 limit`, `429` (more than 1 poll per second for the same session id) |
| `POST /v1/web-sessions/{id}/inspect` | account (app) | `{approve_key}`: `200 {"name", "user_agent", "country", "created_at", "expires_at"}`; `country` is an ISO 3166 code or `null`. The app shows it with "Only approve a code you opened yourself" | `403 forbidden`, `404 not_found`, `422` |
| `POST /v1/web-sessions/{id}/approve` | account (app) | `{approve_key}`: `200 {"approved": true}` | `403 forbidden`, `403 limit`, `404 not_found`, `422` |
| `POST /v1/entitlements/redeem` | account (manager) | `{code}`: `200 {"plan", "source", "expires_at"}` | `404 invalid_code`, `409 already_entitled` (the invite is not spent), `403 forbidden`, `422`, `429` |

Every route that takes an account token can also answer `401 unauthorized`, and the ones that read a body `422 invalid`
(the message names the field, never its value).

Things worth knowing:

- **Enrolling the watch.** The app asks for an enroll code, hands it to the watch (WatchConnectivity), and the watch
  calls `POST /v1/devices/enroll`. A leaked code can only create a `watchos` device, which cannot rotate the key or
  delete the account. An enroll that hits the device limit still spends the code; the app makes another one.
- **Web sessions.** The PWA shows a QR code with the session id and the approve key, and keeps the poll key to itself.
  The iPhone inspects the session (name, user agent, country, time), the user approves, and the PWA gets its tokens on
  the first poll after the approval; nothing is kept in clear in the database. The poll is limited per session id (1 a
  second), not per address, so someone who saw the QR code can make the PWA's polls answer `429` (nothing leaks,
  and the PWA tries again). A session that is not approved in 300 seconds is gone.
- **Sign out everywhere.** `POST /v1/account/key/rotate` with the same key as both the current and the new key signs
  out every other device and drops the open enroll codes.
- **Taking the key back.** Whoever sets the key first is not its owner forever: a user who lost iCloud Keychain, or
  whose key was set by someone holding a stolen sign-in, signs in with Zitadel again on the iPhone and calls
  `POST /v1/account/key/rotate` with `zitadel_token` and a new key. The Cloud checks that the token is fresh, comes
  from the iOS app and belongs to an identity of the caller's account, then sets the key and signs out every other
  device. An account with no Zitadel identity can only rotate with its current key.
- **A lost refresh response.** The device is signed out (no grace window). A device with the account key signs in
  again by itself; the PWA and a watch enrolled by code need to be approved or enrolled again.
- **Concurrent refreshes.** One device does one refresh at a time (the app and its extensions share the token).

### Limits

| What | Limit | Answer |
|---|---|---|
| silent accounts per client address | 3 an hour, 10 a day | `429 rate_limited` with `Retry-After` |
| silent accounts, all clients | 200 in 24 hours (a warning is logged above 100) | `429` with `Retry-After: 3600` |
| attempts per client address on `POST /v1/devices`, `POST /v1/devices/enroll`, `POST /v1/web-sessions`, `POST /v1/account/key/rotate` and `POST /v1/entitlements/redeem` | 20 a minute | `429` with `Retry-After` |
| polls of one web session | 1 a second | `429` with `Retry-After: 1` |
| devices per account | 10 | `403 limit` |
| open enroll codes per account | 5 | `403 limit` |

The limits per address and per web session are kept in memory (one replica; the counters restart with the process,
which is accepted). An IPv4 address counts whole and an IPv6 address by its /64, which one client usually holds. The
global count of new accounts is read from the database, so it survives a restart. Only silent accounts count: signing
in with Zitadel is already a barrier. Set `WRISTCALL_CLOUD_CLIENT_IP_HEADER` behind a proxy, or every client shares
the proxy's address and the limits hit the first users of a mobile network.

An anonymous account (no identity, no plan) unused for `WRISTCALL_CLOUD_ACCOUNT_IDLE_DAYS` is deleted with its
agenda and its cloud-mode data, and so is a device unused for that long (checked every 6 hours). Accounts from 0.3.x and accounts with a
Zitadel identity are never deleted by this.

## Cloud nodes

Since 0.5.0 the Cloud keeps a registry of **nodes**, the servers it vouches for in cloud mode, and serves them an
internal API under `/v1/node/` that holds what cloud mode stores for an account: its agents, the history of its calls,
the push targets of its devices and the month's usage against the quota. The Cloud never opens what a node sends: the
agents' `spec` and the history text arrive sealed by the node, and a search works on hashes the node computed. Without
the signing key and `WRISTCALL_CLOUD_PUBLIC_URL` the node routes answer `401` for a missing or bad node credential
and `404 not_configured` otherwise, except `POST /v1/node/heartbeat`, which works without them. The routes of the
other sections answer as before, with two additions: `POST /v1/server-tokens` refuses a node's URL, and deleting an
account also deletes its cloud-mode data. With no node registered the node routes are unreachable.

A node is registered with `admin nodes add` (see [Admin CLI](#admin-cli)): `id` (1 to 32 lowercase letters, digits or
hyphens, starting with a letter), `url` (https, in the same canonical form as an audience; unique), `region` (three
lowercase letters), `kind` (`always_on` or `on_demand`), `capabilities` (`operator_stt`, `operator_tts`) and
`priority` (lower first). Errors of every node route are `{"error": code, "message": ...}` and never echo the secret,
a token, a URL or text the caller sent.

### Authentication of a node

A request carries up to three credentials, each checked where it belongs:

- **The node's secret**, on every node route: `Authorization: Bearer wc_node_...`. Missing, malformed, unknown or
  disabled: `401 unauthorized`. After a rotation the previous secret works for 15 minutes. A node over
  `WRISTCALL_CLOUD_NODE_REQUESTS_PER_MINUTE` gets `429 rate_limited` with `Retry-After`.
- **The user token**, in `X-Wristcall-User-Token`, on the routes that act for a user's device: an ES256 token the Cloud
  signed (`typ` `wc-server+jwt`, `iss` `WRISTCALL_CLOUD_PUBLIC_URL`, `aud` the node's own URL) with `sub` the account,
  `did` the device and `plt` its platform (`ios`, `ipados`, `macos`, `watchos` or `web`). A token made for another node,
  one that is expired (a minute of clock difference is tolerated) or one that is not good is `401 unauthorized`; a
  device the account no longer has is `403 device_revoked`. This version only verifies it: `POST /v1/server-tokens`
  refuses a node's URL.
- **The call grant**, in `X-Wristcall-Call-Grant`, on the routes that work on one call: a token (`typ`
  `wc-grant+jwt`, `iss` and `aud` the Cloud's URL) the Cloud returns when it opens the call, with `sub`, `did`, `plt`,
  `call`, `node`, `iat`, `exp` and `jti`. Only the node it was issued to can present it, and only for that call (`401`
  otherwise). `exp` is the call's `max_s` plus one hour after `iat`; entries, status `PATCH` and `finish` are still
  accepted up to 24 hours after `exp`, because the node's queue may deliver them late.

The three token types share the Cloud's signing key and are told apart by `typ`: a grant is not a user token and an
account access token is neither. A call that belongs to another account or another node is `404 not_found`.

### Node routes

Reading agents is open to every platform; writing them needs an app or the web (`403 forbidden` for the watch) and a
valid plan (`402 plan_required`), except deleting an agent, which needs no plan. History listing, search, export,
delete and redelivery are for apps and the web too. The watch reads only its own calls.

| Route | Credentials | Body | Answers |
|---|---|---|---|
| `GET /v1/node/me` | node, user | | `200 {"account", "plan", "device": {"id", "platform"}, "agents": [Agent], "usage": Usage}`; `plan` is `null` without one |
| `GET /v1/node/agents` | node, user | | `200 {"agents": [Agent], "etag"}` with an `ETag` header; `If-None-Match` with it is `304` |
| `GET /v1/node/agents/{id or slug}` | node, user | | `200 Agent`; `404 not_found` |
| `POST /v1/node/agents` | node, user | `{id, slug, display_name, icon, call_type, spec, color?}` | `201 Agent`; `403 limit` over `WRISTCALL_CLOUD_MAX_CLOUD_AGENTS`; `409 conflict` for a repeated id or slug; `422 invalid` |
| `PATCH /v1/node/agents/{id}` | node, user | any of `slug`, `display_name`, `icon`, `color`, `call_type`, `spec` | `200 Agent`; `404`; `409`; `422` |
| `DELETE /v1/node/agents/{id}` | node, user | | `204`; `404`. The agent's history stays |
| `PUT /v1/node/agents/order` | node, user | `{agent, index}` | `200 {"agents", "etag"}` with the agent moved to `index` (counted from 0, clamped); `404` |
| `POST /v1/node/calls` | node, user | `{agent, call_type, request_id?, delivery?}` | `201 {"call_id", "grant", "agent", "plan", "quota_s", "max_s", "started_at"}` |
| `POST /v1/node/calls/{id}/entries` | node, grant | `{"entries": [Entry]}` | `204`; `422` |
| `PATCH /v1/node/calls/{id}` | node, grant | any of `status`, `error`, `attempts`, `last_http_status`, `ended_at`, `finished_at`, `delivery` | `204`; `422` |
| `POST /v1/node/calls/{id}/finish` | node, grant | `{status, seconds, error?}` | `204`; `422` |
| `GET /v1/node/calls` | node, user | query: `agent`, `since`, `until`, `before_at` and `before_id` (together), `limit`, `terms` | `200 {"calls": [Call]}` newest first |
| `GET /v1/node/calls/export` | node, user | query: `agent`, `since`, `until` | `200` NDJSON |
| `GET /v1/node/calls/{id}` | node, user | | `200 {"call": Call, "entries": [Entry]}`; `404` |
| `DELETE /v1/node/calls/{id}` | node, user | | `204`; `404`; `409 call_open` |
| `DELETE /v1/node/calls?agent=<id>` or `?all=true` | node, user | | `200 {"deleted": n}`; the open calls stay; `422` with neither |
| `POST /v1/node/calls/{id}/redeliver` | node, user | | `200 {"call", "entries", "agent", "grant"}`; `409 not_one_way`, `call_open`, `busy`, `not_failed` |
| `PUT /v1/node/push-targets` | node, user | `{push_key}` | `204`; `422` when the key is not a registration of this Cloud |
| `DELETE /v1/node/push-targets` | node, user | | `204` |
| `POST /v1/node/push` | node, grant or user | a push message and, with a grant, `call` | `202 {"sent": 0 or 1}` |
| `POST /v1/node/heartbeat` | node | see below | `204` |

**Shapes.** `Agent`: `id`, `slug`, `display_name`, `icon`, `color`, `call_type`, `position`, `spec`, `created_at`,
`updated_at`. `Call`: `id`, `agent_id`, `agent_slug`, `agent_name`, `device_id`, `call_type`, `status`, `error`,
`attempts`, `last_http_status`, `delivery` (`webhook`, `history` or `null`), `created_at`, `updated_at`, `ended_at`,
`finished_at`, `expires_at`, `seconds`, `open`.
`Entry`: `seq`, `role`, `text`, `sealed`, `error`, `at`. `Usage`: `month` (`YYYY-MM`), `used_s`, `quota_s`,
`remaining_s`. Times are seconds since the epoch.

**Agents.** The node generates the id (`ag_` and 12 hex characters), because it seals the agent's secrets with the id
in the authenticated data. `slug` is 1 to 32 characters of `[a-z0-9-]`, starting with a letter or digit, unique per
account; `display_name` 1 to 64 characters, no control characters; `icon` up to 64 characters of dotted lowercase
words; `color` as in the agenda or `null`; `call_type` `conversation`, `one-shot` or `monologue`. `spec` is an object
of at most 256 KiB and 32 levels, stored as sent; the Cloud reads only `spec.retention_days` (1 to 36500, `"forever"`
or `null`). Changing it moves the expiry of the agent's calls and entries.

**Opening a call.** `agent` is an id or a slug, and `call_type` must be the agent's (`422`). The account needs a plan
(`402 plan_required`) and minutes left in the month (`402 quota_exceeded`), and may have one open call
(`409 too_many_calls`, enforced by a unique index). `max_s` is the type's limit (`WRISTCALL_CLOUD_MAX_CONVERSATION_S`,
`WRISTCALL_CLOUD_MAX_ONE_WAY_S`) or what is left of the quota, whichever is less; `quota_s` is what was left when the
call opened. `request_id` (8 to 64 characters of letters, digits, `-` and `_`) makes a retry safe: the same id from the
same node and device while that call is open answers `200` with the same call and a new grant; for another agent or
call type it is `409 conflict`. The call is kept for the agent's `retention_days` (default
`WRISTCALL_CLOUD_HISTORY_RETENTION_DAYS`, ceiling `WRISTCALL_CLOUD_MAX_HISTORY_RETENTION_DAYS`); then the TTL indexes
drop it with its entries.

**Entries.** Up to 100 per request, `seq` from 0 to 100000 and not repeated in the request; `role` is `user` or
`agent`; `at` a number; `text` a string of up to 256 KiB or `null`, and when it is a string `sealed` must be `true`
(`422` otherwise: the Cloud stores no text in clear); `error` a lowercase code or `null`; `term_hashes` up to 2000
hashes of the form `x` and 16 hex digits. Sending an entry again is harmless: the first one stays.

**Status and finish.** Statuses are `recording`, `processing`, `delivered`, `failed`, `empty` and `ended`; `finish`
takes all but `recording`, a `seconds` number and an optional `error` code. A `PATCH` takes `attempts` from 0 to 10000
and `last_http_status` `null` or 100 to 599. `delivery` (on `POST /v1/node/calls` and `PATCH`) is `webhook`,
`history` or `null` (the default; a call written before 0.5.1 reads `null`); a `request_id` replay ignores it, and
redelivery leaves it as it was. `finish` closes the call, frees the account's open slot and adds its
seconds to the month's usage (the month the call began in), once: repeating it counts once, and one that arrives after
the Cloud closed the call as an orphan corrects its status, error and seconds, moving the usage by the difference.
`seconds` is rounded up to whole seconds and kept within `max_s` plus a minute.

**Quota.** `WRISTCALL_CLOUD_QUOTA_MINUTES` per account per calendar month (UTC), the same for `full` and `custom`, all
call types together, measured in the wall-clock seconds nodes report; there is no overage. The first time a month's
usage reaches 80%, every push target of the account gets a `quota.warning` event (`data`: `month`, `used_s`,
`quota_s`), once per month. A registration receives it only if it asked for that event (the push relay's `events`).

**History.** `terms` can be repeated; each is a group of up to 16 comma separated hashes and a call must have an entry
matching one hash of every group (up to 16 groups). `limit` is 1 to 200 (default 50); `before_at` and `before_id` page
from the last call of the previous page. `since` is inclusive and `until` exclusive, on `created_at`. An open call is
never deleted (deleting one answers `409 call_open`, deleting many skips it), so a call cannot be erased to dodge
the quota. The export
writes one line `{"call", "entries"}` per call, oldest first, and ends with `{"end": true, "count": n}`; a stream
without that line was cut. Redelivery follows the server's rules: only one-shot and monologue calls, not while
`processing`, and only a `failed` call whose `error` is `delivery_failed` or `interrupted`; the call becomes the
requesting node's, `processing` again, and the answer carries a new grant for it.

**Push.** A node sends a notification only to the device it acts for, by the call's grant (the grant must not be
expired) or by the user token. The body is a message as in [Push relay](#push-relay) (`event`, `title`, `body`,
`data`...), plus `call` with a grant; `sent` is `0` when the device has no push target or its registration did not ask
for the event. A target is the push registration a device made, kept by id: the Cloud never keeps the key.

**Heartbeat.** Every 15 seconds or so a node sends `{boot_id, version, active_call_ids, active_by_type, capacity,
providers_ok}`: `boot_id` and `version` strings (up to 64 and 32 characters); `active_call_ids` up to 500 ids;
`active_by_type` and `capacity` objects with `conversation` and `one_way` (whole numbers from 0; `capacity` also takes
`null` for no limit); `providers_ok` a boolean. The Cloud keeps the last one on the node's document. Calls the Cloud
holds open for the node that it does not list, and that are older than 30 seconds, are orphans: a one-shot or monologue
call becomes `failed`, a conversation `ended`, with `error` `interrupted`, counting the seconds up to the heartbeat
before. A node that stays silent for 120 seconds (counted from its last heartbeat or the Cloud's own start, whichever is
later, so a Cloud that was down does not blame the nodes) has all its open calls closed the same way by a sweeper
that runs every 30 seconds. When the `boot_id` changes, or the node goes silent, its calls left `processing` become
`failed` with `error` `interrupted`, because nothing is processing them any more. Since 0.7.0 a heartbeat may also
carry the node's counters, `stats` (server 0.11.0 or later), which go into the hourly summary below.

### Node stats

Since 0.7.0 the Cloud keeps an hourly summary of each node in `node_stats`, like a shift log with one page per node and
per hour (UTC): every heartbeat the Cloud accepts adds to the page of the hour it arrived in. A page holds `beats` (the
heartbeats accepted), `peak` and `capacity` by call type (the most calls open at once, from `active_by_type` and
`stats.peak`, and the most slots the node offered that hour; a `null` capacity, no limit, is left out), and the sum of
the counts in `stats`. Pages are dropped 90 days after their hour (a TTL index on `purge_at`).

`stats` is optional and has the counts of the node since the last heartbeat the Cloud accepted (see "Node metrics" in
the [server README](../README.md)): `calls`, `busy` and `peak` by type (`conversation`, `one_way`), `not_ready`,
`cloud_unreachable_s` and `outbox_dropped` (whole numbers), `first_audio_ms` (a histogram: counts per bucket, keyed by
the bucket's upper bound in ms, `250` to `15000`, then `inf`), `stt_ms` and `tts_ms` (a histogram per provider) and
`provider_failures` (a count per provider). Provider names are lowercase letters, digits, `-` and `_` (up to 32), at
most 8 per map. A heartbeat without `stats` (a node before 0.11.0, or the heartbeat of a node that is draining) still
counts in `beats` and `peak`. `stats` that do not follow these rules are ignored: the heartbeat is accepted as usual
(`204`) and counts in `beats` and `peak`, and the log says why they were ignored, never what they held. A key the
Cloud does not know is skipped, so a newer node does not lose the rest. Metrics never fail a heartbeat: a failed write
of the summary is logged and the answer stays `204`.

The node takes its counts off once a heartbeat is accepted, so nothing is lost or counted twice while the Cloud refuses
heartbeats. One case does count twice, and that is accepted: a heartbeat the Cloud stored whose answer never reached the
node (a dropped connection), because the node sends the same counts again.

The route counts too, on pages of its own (`node: "_route"`): `routes_call`, the call routes issued, and `no_node`, the
call routes refused with `503 no_node_available` (`purpose: manage` is not counted). A failed count changes no answer.

`wristcall-cloud admin nodes stats [NODE] [--hours N]` (see [Admin CLI](#admin-cli)) reads the summary of the last
`N` hours (24 by default, at most 2160, the current hour included), for one node or every enabled one:

| Line | What it shows |
|---|---|
| `node`, `hours` | the node and the window |
| `calls`, `busy` | calls taken and calls refused because every slot was taken, by type |
| `not_ready` | calls refused because the node was not ready or was draining |
| `busy_pct` | `busy` / (`calls` + `busy`), in %; `n/a` when both are 0 |
| `peak` | the most calls open at once, by type |
| `first_audio_p50_ms`, `first_audio_p95_ms` | the time to the first audio of an answer, median and 95th percentile: the upper bound of the bucket the percentile falls in (`>15000` past the last one); `n/a` without samples |
| `stt_p95_ms`, `tts_p95_ms` | the 95th percentile per provider (`name=ms ...`, `none` without samples) |
| `provider_failures` | per provider (`name=n ...`, `none` without failures) |
| `cloud_unreachable_s`, `outbox_dropped` | sums |
| `hours_without_heartbeat_day` | hours that have ended, start between 07:00 and 23:00 in Brasília (America/Sao_Paulo) and have no heartbeat; hours before the node was registered do not count |
| `days_at_peak` | days (Brasília) with an hour where the conversations open reached the conversation slots the node offered that hour |
| `routes_call`, `no_node`, `no_node_pct` | the route's counters over the window, once after the nodes (all nodes together); `no_node_pct` is `no_node` / (`routes_call` + `no_node`), in %, `n/a` when both are 0 |

## Routes and the Cloud entry

Since 0.6.0 an app asks the Cloud which node serves it, and the Cloud adds a **Cloud entry** to the agenda of an account
with a plan. Both need the per-server token variables (`WRISTCALL_CLOUD_PUBLIC_URL` and the signing key): without the key
`POST /v1/route` answers `404 not_configured`, and without the public URL nothing is provisioned. With no node
registered the route answers `503 no_node_available`.

### The route

`POST /v1/route` takes an account session (the access token of a device; a Zitadel token is `401`). Errors are
`{"error": code, "message": ...}` as everywhere, and never echo a token or a node URL.

| Body | Meaning |
|---|---|
| `purpose` | `call` (a node to run a call) or `manage` (a node to read and change the account's cloud-mode agents and history) |
| `agent` | the `ag_...` id of one of the account's cloud-mode agents. Required for `call`, refused for `manage` |
| `avoid` | optional list of up to 8 node ids that just failed for the caller |

```json
{"purpose": "call", "agent": "ag_0123456789ab", "avoid": ["away"]}
```

The answer is the chosen node, a per-server token for it and, when a second node can take the request, a fallback with
its own token (`null` otherwise):

```json
{
  "node": {"id": "home", "url": "https://node-a.example.com", "region": "sao"},
  "token": "eyJ...",
  "expires_at": 1792580000,
  "fallback": {
    "node": {"id": "away", "url": "https://node-b.example.com", "region": "iad"},
    "token": "eyJ...",
    "expires_at": 1792580000
  }
}
```

| Status | When |
|---|---|
| `200` | a node was chosen |
| `401 unauthorized` | no session, a Zitadel token, or a device that was removed |
| `402 plan_required` | a `call` without a plan; a `manage` without a plan from an account that never had the Cloud entry. A lapsed plan can still `manage`, so the history stays readable |
| `403 device_revoked` | the device was removed between the authentication and the route (rare; a removed device normally gets `401`) |
| `404 not_found` | `agent` is not one of the account's cloud-mode agents |
| `404 not_configured` | the Cloud has no signing key |
| `422 invalid` | an unknown field, a bad `purpose`, `agent` missing or sent with `manage`, or a bad `avoid` (the message never echoes the values) |
| `503 no_node_available` | no node can take it now; `Retry-After: 30` |

The token is the per-server token of [Per-server tokens](#per-server-tokens), signed `wc-server+jwt` with the audience set
to the chosen node's URL (so it works on that node only), the `did` of the device, the `plt` of the platform stored for
that device, the `client_id` of the session and a lifetime of 300 seconds. It is not the token `POST /v1/server-tokens`
issues: that route still refuses the URL of a registered node.

**Node choice.** The Cloud reads the nodes from the database on every request and keeps the nodes that pass these
checks:

- the node is `enabled` and not in `avoid`;
- on a `call` of an account on the `full` plan, the node has both `operator_stt` and `operator_tts` and its heartbeat did
  not report `providers_ok: false` (this check does not apply to other plans);
- an `always_on` node is healthy when its last heartbeat is less than 45 seconds old. During the first 60 seconds after
  the Cloud starts a node last heard from up to 120 seconds before the start also counts, because nodes had no chance to
  send a heartbeat yet. On a `call`, the node must also have room: `active_by_type` below `capacity` for the kind of the
  agent (`conversation`, or `one_way` for one-shot and monologue), unless that `capacity` sets no limit for that kind
  (missing or `null`);
- an `on_demand` node (it has no heartbeat) is healthy unless 3 different accounts put it in `avoid` within the last 300
  seconds. Failure reports are kept in memory, so a restart forgets them, and ids that are not registered nodes are
  ignored.

The nodes left are ordered by the position of their `region` in the row of the caller's country (a region outside the row
goes last), then by `priority` (lower first), then by load (calls in progress), then by `id`. The first is `node`, the
second is `fallback`. The country comes from `WRISTCALL_CLOUD_CLIENT_COUNTRY_HEADER` (`CF-IPCountry` behind Cloudflare);
without it, or for a country not in the table, the `*` row is used. The default table:

| Country | Regions, best first |
|---|---|
| `BR` | `sao`, `gru` |
| `US`, `CA`, `MX` | `iad` |
| every other country (`*`) | `iad` |

Change it with `WRISTCALL_CLOUD_ROUTE_REGIONS`. A `home` node registered with `--region sao` therefore serves Brazil first
and everyone else second, after a node in `iad`.

### Which apps see the Cloud entry

Every app sends `X-Wristcall-Client: <platform>/<x.y.z>` (platform `ios`, `ipados`, `macos`, `watchos` or `web`, for
example `ios/0.3.0`). The Cloud shows the Cloud entry only to a caller whose header is well formed, names the same
platform as the session's token, lists that platform in `WRISTCALL_CLOUD_CLOUD_ENTRY_CLIENTS` and reaches the minimum
version. With the variable empty, or for any older app (which does not send the header), the entry does not exist: it is
absent from `GET /v1/servers` and `GET /v1/agents`, `GET /v1/servers/{id}` answers `404`, and the `servers` count of
`GET /v1/account` leaves it out. The header is allowed by [CORS](#browsers-cors).

### The Cloud entry

The Cloud entry is a server of the agenda that only the Cloud creates, one per account with a plan:
`{"name": "Wristcall Cloud", "kind": "cloud", "url": <WRISTCALL_CLOUD_PUBLIC_URL>, "linked": true}`, kept in the account's
servers with `managed: true`. Its agents are not stored in it: they are the account's cloud-mode agents (the ones of
[Node routes](#node-routes)), read when the entry is shown, as `{id, slug, display_name, icon, call_type}` and `color`
when set. A visible entry in `GET /v1/servers` looks like this:

```json
{
  "id": "srv_0123456789ab",
  "name": "Wristcall Cloud",
  "url": "https://cloud.example.com",
  "kind": "cloud",
  "linked": true,
  "agents": [
    {"id": "ag_0123456789ab", "slug": "voice-notes", "display_name": "Voice notes", "icon": "note.text", "call_type": "one-shot", "color": "yellow"}
  ],
  "created_at": 1792576000.0,
  "updated_at": 1792576000.0
}
```

The Cloud owns the entry: `PATCH`, `DELETE` and `PUT .../agents` on it answer `409 managed_by_cloud` to an app that sees
it, and `404 not_found` to one that does not. `POST /v1/servers` with `kind: "cloud"` answers `422 invalid`. The entry does
not count against `WRISTCALL_CLOUD_MAX_SERVERS`, and it goes away only with the account. Revoking a plan removes neither
the entry nor the agents, so the history stays readable until its retention ends. Entries with `kind: "cloud"` that apps
made before 0.6.0 (no `managed` field) are ordinary servers and keep working as they did: visible, editable, with their
own agents.

### Voice notes

An account on the `full` plan also gets one agent, **Voice notes**: `slug` `voice-notes`, `icon` `note.text`, `color`
`yellow`, `call_type` `one-shot`, and a `spec` of `{"language": ..., "stt": {"provider": "<WRISTCALL_CLOUD_VOICE_NOTES_STT>"},
"action": null, "retention_days": null}`. Nothing in it is a secret, so nothing is sealed: the node reads the provider
name in clear and uses its own speech to text provider with that name (`stt-default` unless you change the variable), so
the node of the `full` plan must name its provider the same. The language is `pt` when the country of the request (the
redeem or the first route) is Portuguese-speaking (`BR`, `PT`, `AO`, `MZ`, `CV`, `GW`, `ST`, `TL`), otherwise
`WRISTCALL_CLOUD_VOICE_NOTES_LANGUAGE`; `admin grant` and `admin provision` take `--language` to choose it. The user can
change the language later in the app.

The agent is created once per account: the entry records it (`voice_notes_at`), so an account that deletes "Voice notes"
does not get it back at the next `grant` or `provision --all`. An account that already has an agent with the slug
`voice-notes` keeps it.

### Provisioning

The Cloud entry (and "Voice notes" on `full`) is created, safely and at most once, when:

- an account redeems an invite (`POST /v1/entitlements/redeem`). If provisioning fails the redeem still answers `200`
  (the invite is spent) and the failure is logged;
- an operator runs `admin grant`, or `admin provision <account>` or `admin provision --all`;
- an account with a plan and no entry calls `POST /v1/route`, which repairs a provisioning that failed.

If the account already has a server of its own at the Cloud's URL, no entry is created (that server is never turned into
a managed one), and a warning is logged.

## Transition flag

`WRISTCALL_CLOUD_LEGACY_ZITADEL_TOKENS=1` keeps the Cloud working for the app builds that predate account sessions: the
PWA 0.1.0 to 0.2.x (they sent the Zitadel token on every call; PWA 0.3.0 migrates its session to a Cloud web device),
the iPhone app `ios-v0.1.0` and the watch app `watch-v0.4.0`. With it on, a Zitadel token is accepted on
the routes that existed in 0.3.x (`GET` and `DELETE /v1/account`, the servers and agents routes, `GET /v1/agents`,
`POST /v1/server-tokens`), with the same answers as 0.3.1 and the same account (`sub` of the per-server token
included). The account is created on first use, as before, with the id `<issuer>#<subject>`. The routes of
[Accounts and sessions](#accounts-and-sessions) never take a Zitadel token, except the exchange at `POST /v1/devices`
and the key reset proof (`zitadel_token`) in the body of `POST /v1/account/key/rotate`.

The default is off. Turn it off once the iPhone app 0.2.0 or later, the watch app 0.5.0 or later and the PWA 0.3.0 are
published and in use: from then on a Zitadel token only works for the exchange, and old builds get `401`. The Cloud logs a warning at every start while it is on,
and `/v1/config` reports it (`sessions.legacy_zitadel_tokens`).

## Admin CLI

`wristcall-cloud admin` works straight on the database, for beta plans, invite codes and numbers. It needs
`WRISTCALL_CLOUD_MONGO_URL` (or `_FILE`) and, if it is not the default, `WRISTCALL_CLOUD_DATABASE`; nothing else of the
service configuration. Output is one `key: value` line per item on stdout; errors go to stderr.

| Command | What it does |
|---|---|
| `admin show <account>` | account, kind, created_at, last_seen_at, devices, identities, plan (and plan_expires_at), then cloud_entry (`true` or `false`), cloud_agents, cloud_calls and usage_month_s (call seconds this month, UTC) |
| `admin grant <account> --plan full\|custom [--days N] [--language L]` | gives the account a beta plan, replacing any; without `--days` it has no end. Then provisions the account (Cloud entry and, on `full`, "Voice notes", see [Routes and the Cloud entry](#routes-and-the-cloud-entry)) and prints `provisioned: entry=true\|false voice_notes=true\|false` (what this call created), or `provisioned: skipped` when `WRISTCALL_CLOUD_PUBLIC_URL` is not set. `--language` is the language of "Voice notes" (default from `WRISTCALL_CLOUD_VOICE_NOTES_LANGUAGE`) |
| `admin provision <account> [--language L]` | gives an account that has a plan what the plan includes: the Cloud entry and, on `full`, "Voice notes" (once). Safe to repeat. Prints `provisioned: entry=... voice_notes=...`. Exit `1` for no such account or no plan; exit `2` without `WRISTCALL_CLOUD_PUBLIC_URL` |
| `admin provision --all [--language L]` | the same for every account with a plan that has not ended; prints `accounts`, `entries_created` and `voice_notes_created` |
| `admin revoke <account>` | takes the plan away (`revoked: true` or `false`) |
| `admin invites create --plan full\|custom [--count N] [--days N]` | prints `N` new single-use codes (1 to 1000, default 1) that can be redeemed for `--days` days (default 30). This is the only time the codes exist in clear |
| `admin stats` | counts: accounts, anonymous_24h, devices, identities, entitlements, entitlements_by_country (since 0.7.0: the plans in force per country, sorted, `BR=1,unknown=2`; `unknown` for those with no country recorded, `none` without any plan), then schema (the database's `meta` version, `none` before the first start), nodes, nodes_enabled, cloud_calls_open (calls open right now) and usage_month_s (call seconds this month, all accounts) |
| `admin nodes add <id> --url URL --region xxx --kind always_on\|on_demand [--capability operator_stt] [--capability operator_tts] [--priority N] [--allow-loopback]` | registers a node and prints `node`, `url` and `secret`. The secret (`wc_node_` and 43 characters) is the only time it exists in clear: the database keeps its SHA-256. Exit `2` for a bad id, URL, region or kind, a loopback URL without `--allow-loopback`, or an id or URL already registered |
| `admin nodes list` | one `node: <id> url=... region=... kind=... enabled=... last_seen_at=...` line per node, by priority (lower first), then id |
| `admin nodes rotate <id> [--now]` | prints a new `secret`; the old one keeps working for 15 minutes (a node deploy is not atomic). `--now` cuts the old one at once (a leak). Exit `1` for an unknown node |
| `admin nodes enable <id>` / `disable <id>` | prints `enabled: true` or `false`; a disabled node's secret is refused (`401`). Exit `1` for an unknown node |
| `admin nodes stats [<id>] [--hours N]` | the hourly summary of the last `N` hours (default 24, up to 2160) for one node, or for every enabled node: one block of lines per node, then the route's counters (see [Node stats](#node-stats)). Exit `1` for an unknown node |

Every `admin nodes` command creates the indexes first, so a repeated URL is refused even before the Cloud has started
on 0.5.0.

The `provision` and `grant` commands read `WRISTCALL_CLOUD_PUBLIC_URL` (the Cloud entry points at it) and the two
`WRISTCALL_CLOUD_VOICE_NOTES_*` variables; set them the way the service has them.

Exit codes: `0` done, `1` no such account (or, for `provision`, no plan), `2` bad usage or configuration, `3` the
database failed (only the exception type is printed).

In a container, with the connection string in a secret (the service then sets `WRISTCALL_CLOUD_MONGO_URL_FILE`, not
`WRISTCALL_CLOUD_MONGO_URL`: both together are a configuration error, and the service does not start):

```sh
docker exec <container> wristcall-cloud admin stats
docker exec <container> wristcall-cloud admin grant acc_xxxxxxxxxxxxxxxxxxxxxxxxxx --plan full --days 30
docker exec <container> wristcall-cloud admin invites create --plan full --count 5
docker exec <container> wristcall-cloud admin nodes add home --url https://node.example.com --region sao --kind always_on
docker exec <container> wristcall-cloud admin provision --all
```

## Data and migration

Collections of the `wristcall_cloud` database (`WRISTCALL_CLOUD_DATABASE`):

| Collection | Holds |
|---|---|
| `accounts` | the 0.3.x collection, with new optional fields: `kind` (`anonymous` or `zitadel`; none on accounts from 0.3.x), `key_hash`, `key_set_at`, `last_seen_at` |
| `servers` | unchanged (the agenda) |
| `identities` | `(kind, issuer, subject)` to account, unique |
| `devices` | one per session: account, name, platform, refresh token digests, `last_seen_at` |
| `enroll_codes`, `web_sessions`, `invites` | digests of the secrets. Expired enroll codes and web sessions are dropped by TTL indexes (an hour after they expire); invites are kept, an expired one just cannot be redeemed |
| `entitlements` | the plan, one per account (`_id` is the account); `country` (since 0.7.0) is the caller's country when an invite was redeemed, `null` for `admin grant` and before 0.7.0 |
| `meta` | `_id: "schema"`, `version: 3`, `migrated_at` |
| `nodes` | the registered nodes: `_id`, `url` (unique), `region`, `kind`, `capabilities`, `priority`, `enabled`, `secret_hash` (unique; SHA-256 of the secret), the previous secret's hash and its end while a rotation is in grace, and what the last heartbeat said |
| `cloud_agents` | the cloud-mode agents: `_id` (the id the node generated), `account`, `slug` (unique per account), `position`, `spec` |
| `cloud_calls` | one per call: account, device, node, agent, status, seconds, `open` (only while the call is open: a unique partial index allows one per account), `purge_at` (TTL index) |
| `cloud_call_entries` | the history of a call, one per `(call, seq)`: sealed text and term hashes; `purge_at` (TTL index) |
| `cloud_push_targets` | where an account's devices get notifications (`(account, device)` to a push registration's id, never its key) |
| `usage` | one document per `(account, month)`: `total_s`, `seconds_by_type`, a `by_call` ledger, `warned_80` |
| `node_stats` | the hourly summary of the nodes, one document per `(node, UTC hour)` (`_id` `<node>\|<YYYY-MM-DDTHH>`) and the route's counters (`node: "_route"`); `purge_at` (TTL index, 90 days after the hour). See [Node stats](#node-stats) |

The push registrations keep their collection, unchanged. The Cloud never opens what the nodes send: the history text
and the agents' `spec` are sealed by the node, and searching works on hashes the node computed.

**Migration.** At every start, before serving, the Cloud creates the indexes and then looks for accounts whose id is
`<issuer>#<subject>` and creates the Zitadel identity of each one that has none. That is all: no account is changed.
It is idempotent (a second start creates nothing), and the log says how many identities were created. Then it writes
`meta.schema` `version` 3 (it was 2 until 0.4.1).

**Going back to 0.3.1.** 0.3.1 ignores the new collections and fields, so going back needs no restore of the database.
The accounts, servers and agents are intact. Going forward again later creates only the identities that are missing.
Account sessions do not work while 0.3.1 runs (it does not know them), so apps that only have a session cannot use
the Cloud until 0.4.0 is back.

**Schema 3 (0.5.0).** The only change is the collections and indexes of cloud mode (`nodes`, `cloud_agents`,
`cloud_calls`, `cloud_call_entries`, `cloud_push_targets`, and an index on `usage`, a collection that is new too):
no existing document changes. Dump the `wristcall_cloud` database before deploying all the same. **Going back to
0.4.1** needs no restore: it ignores the new collections, and the accounts, devices, plans and agenda are as they were.
Going forward again finds the cloud-mode data where it was.

**0.7.0.** Adds the `node_stats` collection and its indexes, created at start; no existing document changes and the
schema version stays 3. **Going back to 0.6.1** needs no restore: it ignores `node_stats` and the heartbeats' `stats`.

## Browsers (CORS)

A browser app (the wristcall PWA) can call the Cloud only from an origin listed in `WRISTCALL_CLOUD_CORS_ORIGINS`.
With the variable empty the Cloud sends no `Access-Control-*` header and browsers refuse every cross origin call, as
before. For a listed origin the Cloud allows the methods `GET`, `POST`, `PUT`, `PATCH` and `DELETE`, the request
headers `Authorization`, `Content-Type` and `X-Wristcall-Client`, caches the preflight answer for 10 minutes and exposes
`Retry-After` (the
`429` of the push registration) to the page.

Credentials are not allowed (`Access-Control-Allow-Credentials` is never sent): the API takes a bearer token in a
header and sets no cookies, so listing known origins is all it needs, and a page on another site cannot ride on a
signed in browser. List the PWA's own origin (the address the PWA is served from), not `*`: wildcards, paths and
`http` outside localhost are refused at start. Error answers (`401`, `404`, `413`, `422`, `503`) carry the headers too;
a `500` does not (Starlette's last resort layer answers outside every middleware), so a browser sees it as a network
failure.

## Rate limiting

The routes that create an account or a device, or take a secret, limit themselves (see [Limits](#limits)). The other
account routes (the agenda, `GET /v1/account`, per-server tokens) do not: the reverse proxy of the deployment (Traefik)
is expected to limit them, per client address, in front of every route. Neither do `POST /v1/session/refresh` (a
refresh token has 256 bits, guessing is not an option) and the polls of web sessions with an unknown id (only polls of
the same id are limited): only the proxy limits those.

The push relay limits itself, in memory (one replica; the counters restart with the process, which is accepted):
`WRISTCALL_CLOUD_PUSH_PER_MINUTE` and `_PER_DAY` per push key, and `WRISTCALL_CLOUD_REGISTRATIONS_PER_MINUTE` per
client address on the anonymous registration. Set `WRISTCALL_CLOUD_CLIENT_IP_HEADER` behind a proxy, or every client
shares the proxy's address.

## Before deploying

- **Cloud 0.5.0.** Dump the `wristcall_cloud` database first (schema 2 to 3, collections and indexes only; see
  [Data and migration](#data-and-migration)). No node is registered at first, so nothing changes for accounts, apps or the
  PWA; the node routes start to answer once `admin nodes add` registers one. Register a node on `https://` (a loopback
  URL needs `--allow-loopback`, for development), keep its secret in a secret store, and note that a URL registered as
  a node can no longer be asked for as the audience of `POST /v1/server-tokens`. Until a node is registered the heartbeat
  sweeper has nothing to do.
- **Cloud 0.6.0.** No schema change and no new collection (the Cloud entry is a document of `servers` with a new `managed`
  field, "Voice notes" one of `cloud_agents`). **Going back to 0.5.1** after
  `admin provision --all` has run needs one step first: 0.5.1 apps would see the managed entry (its URL is the Cloud's
  public URL), pair with every agenda entry and could overwrite its agents. Before rolling back, delete the managed
  entries: `db.servers.deleteMany({kind: "cloud", managed: true})` on the `wristcall_cloud` database. The "Voice notes"
  agents in `cloud_agents` can stay (0.5.1 apps never read them). Caveat: upgrading again and provisioning again
  creates "Voice notes" anew for users who had deleted it, because the `voice_notes_at` mark lived on the deleted
  entry. Run `wristcall-cloud admin provision --all` once after
  deploying, so every account that already has a plan gets its Cloud entry (and "Voice notes" on `full`); accounts
  that redeem or are granted a plan later are provisioned by themselves. Until a node is registered and healthy,
  `POST /v1/route` answers `503 no_node_available`. Nobody sees the Cloud entry until
  `WRISTCALL_CLOUD_CLOUD_ENTRY_CLIENTS` names a version, so current apps (watch 0.5.1, iPhone 0.2.2, PWA 0.3.0) see no
  change. Set it to `ios=0.3.0,watchos=0.6.0,web=0.4.0` (the first apps that handle the entry) once those apps are out. Register the `home` node with `--region sao --capability operator_stt --capability operator_tts`, and name
  its speech to text provider like `WRISTCALL_CLOUD_VOICE_NOTES_STT` (`stt-default`).
- **Replay between servers.** The block that kept the Cloud API from being deployed (a Zitadel token given to a server
  could be replayed here) is lifted only once every server linked to a Cloud account runs 0.6.0 with
  `central_account.audience` set: those servers take only per-server tokens. A server still on 0.5.0 with a
  `central_account` receives the user's Zitadel login token, and whoever operates it can call
  `POST /v1/server-tokens` with that token and mint valid per-server tokens for **any** 0.6.0 server where the same
  user is linked: there they can pair a device (in `attestation` mode) or send approval requests to the user. So
  upgrade every 0.5.0 server with `central_account` to 0.6.0, or unlink its users, **before** setting
  `WRISTCALL_CLOUD_SIGNING_KEY`. Apps must never send a login token to a server, only per-server tokens.
- **The transition flag.** While PWA 0.1.0 to 0.2.x, the iPhone app `ios-v0.1.0` and the watch app `watch-v0.4.0` are in use,
  set `WRISTCALL_CLOUD_LEGACY_ZITADEL_TOKENS=1`, or they get `401`. Turn it off once iPhone 0.2.0 or later, watch 0.5.0
  or later and PWA 0.3.0 are published and in use (see
  [Transition flag](#transition-flag)).
- **Client address and country.** The account limits are per client address: set `WRISTCALL_CLOUD_CLIENT_IP_HEADER`.
  Behind Cloudflare also set `WRISTCALL_CLOUD_CLIENT_COUNTRY_HEADER=CF-IPCountry`, so web sessions show a country.
- **Rate limit at the proxy (Traefik).** `POST /v1/push/registrations` is anonymous, and `POST /v1/push/send` with
  random keys costs one MongoDB lookup each before it is refused (the per key limit only counts known keys). Besides
  the Cloud's own limits, put a limit per client address on both in Traefik, and set
  `WRISTCALL_CLOUD_CLIENT_IP_HEADER` to the header Traefik writes.
- **Keys do not change without a plan.** Changing the VAPID key invalidates every Web Push subscription (each device
  must subscribe again). After the signing key changes, tokens signed with the old key are refused by a server as
  soon as it fetches the new JWKS, and new tokens can fail for up to one minute, until the server fetches it (a
  server looks up an unknown key id at most once every 60 seconds). The APNs key can be replaced by a new one of the
  same team and topics.
- **Account deletion** needs the machine user's secret (Infisical `prod:/servicos/wristcall-cloud`,
  `WRISTCALL_ZITADEL_CLOUD_CLIENT_SECRET`) in `WRISTCALL_CLOUD_ZITADEL_CLIENT_SECRET_FILE`: without it the app's
  "Delete account" leaves the sign-in in place, which Apple's review does not accept.
- **Store the keys as secrets** (`_FILE` variables), and never run with `WRISTCALL_CLOUD_PUSH_FAKE` outside a local test.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
WRISTCALL_CLOUD_TEST_MONGO_URL=mongodb://localhost:27017 .venv/bin/python -m pytest -q
docker build -t wristcall-cloud .
```

Tests need a MongoDB (`WRISTCALL_CLOUD_TEST_MONGO_URL`, default `mongodb://localhost:27017`); each test uses a
throwaway `test_<uuid>` database and drops it. `tests/test_identity_zitadel.py` runs against the real Zitadel when
`WRISTCALL_CLOUD_TEST_ZITADEL_CLIENT_SECRET` (or `_FILE`) holds the machine user's secret: it creates a throwaway user
in the organization and deletes it (issuer, organization and client id default to wristcall's; `_ISSUER`, `_ORG_ID`,
`_CLIENT_ID` change them). Without it the test is skipped.
