# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Each component follows [SemVer](https://semver.org/) on its own: the server is published by the `server-vX.Y.Z` tag (Docker image); the Cloud (`cloud/`) by the `cloud-vX.Y.Z` tag (Docker image); the web app (`web/`) by the `web-vX.Y.Z` tag (Docker image); the watch app is marked by the `watch-vX.Y.Z` tag and the iPhone app by the `ios-vX.Y.Z` tag; neither has a published binary (build them with Xcode, see [watch/README.md](watch/README.md)).

## [Unreleased]

### Changed

- Watch and iPhone apps: the default build points at the project's Cloud, `https://wristcall-cloud.trigram.com.br`
  (`WRISTCALL_CLOUD_URL` in `watch/Config/Base.xcconfig`), now that it is deployed and passed the end to end tests
  against it. Set it empty in `watch/Config/Local.xcconfig` for a build without an account. The push build keeps a
  local Cloud.

## [web-0.2.0] - 2026-10-16

For servers 0.8.0 (link codes) and Cloud 0.3.0 or later.

### Added

- `/link`: opens the QR code of a server (`https://<site>/link#v=1&s=<server>&c=<link code>`, see "Link URL" in
  [docs/protocol.md](docs/protocol.md#link-url)). The page reads the fragment once and drops it from the address bar,
  shows the server's host and version with "Only connect servers you trust", and either connects the server to the
  signed-in account ("Connect", the same calls as "Add server" with a code) or hands the link to the iPhone app ("Open
  in wristcall", `wristcall://link#…`). Signed out, "Sign in to connect here" keeps the link in the tab for 15 minutes
  and the sign-in comes back to it. Without a Cloud only the app link is offered.
- "Add server" with a code also takes a link code (26 characters, any case, with spaces or hyphens).

## [0.8.0] - 2026-10-16

### Added

- Link code: a single use code (16 random bytes, valid 15 minutes, only its SHA-256 stored) that links a Cloud account
  to a user without typing a pairing code or a token. Format in [docs/protocol.md](docs/protocol.md#link-url).
- The startup log shows the QR code and link on a new server (`central_account` set, `public_url` in the audience,
  nobody linked, no user or a single user with no API token and no device). A server already in use gets a hint.
- `wristcall link [--user <handle>]` prints a fresh QR code and link; it refuses (exit 1) when `central_account` is
  missing or `server.public_url` is outside `central_account.audience`. For a user already linked it prints a note on
  stderr (the new code replaces that link) and still issues the code.
- Only the newest link code of a user works: issuing a new one (a restart of a new server, `wristcall link`) or linking
  with one deletes that user's other unused codes.
- Dependency `segno` (pure Python) to draw the QR code.

### Changed

- `POST /v1/account/link` also accepts the long link code, besides the 8 digit pairing code. A wrong long code is
  `401 invalid_code` and does not count as an attempt on pairing codes.
- A server creates the user `owner` at startup when `central_account` is set, `server.public_url` is in
  `central_account.audience` and there are no users, as the `profiles` import does; `wristcall link` does the same on
  a server without users.
- Docs: the web app origin is `https://wristcall-app.trigram.com.br` (it was `app.wristcall.trigram.com.br`).

### Security

- The code in the QR gives the owner's API token to whoever uses it first. It is printed only on a new server and sits
  in the server log: whoever can read the log can use it. A server already in use never prints a code at startup.

### Upgrade note

- Back up `data_dir` first. The database migrates to schema 7 on start (the new `link_codes` table). Rolling back to
  0.7.x needs that backup: 0.7.x refuses the newer schema.

## [cloud-0.4.0] - 2026-10-16

### Added

- Account sessions: the Cloud owns the account. An app makes a silent account from a key generated on the device
  (`POST /v1/accounts`, no sign-in), and every device gets its own session: a one hour access token
  (`wc-account+jwt`, signed with the key of the per-server tokens) and a rotating refresh token
  (`POST /v1/session/refresh`; using a refresh token twice signs the device out at once). Devices are listed and
  removed with `GET /v1/devices` and `DELETE /v1/devices/{id}`. New accounts have an `acc_` id.
- Devices can be added with the account key or with a fresh Zitadel token (`POST /v1/devices`), and the account key can
  be set and rotated (`PUT /v1/account/key`, `POST /v1/account/key/rotate`). The rotation takes `new_account_key` and
  exactly one proof: the current `account_key`, or a fresh `zitadel_token` of the iOS app for a user of the account,
  which resets the key whether or not one exists (a lost iCloud Keychain, or a key set by someone else). Either way
  every other device, open enroll code and approved web session of the account goes. New errors: `401 stale_token`,
  `403 forbidden` (a sign-in of another account or of another app) and `422 invalid` (both proofs or neither).
- Device names refuse control and format characters (newlines, bidi overrides, zero-width characters): `422 invalid`.
- Enroll codes: an app creates a single use code and the watch enrolls itself with it
  (`POST /v1/devices/enroll-codes`, `POST /v1/devices/enroll`). A code only creates a `watchos` device.
- Web sessions: the PWA opens a session, the iPhone inspects it (name, user agent, country, time) and approves it, and
  the PWA gets its tokens on the next poll (`POST /v1/web-sessions`, `GET /v1/web-sessions/{id}`, `.../inspect`,
  `.../approve`). `WRISTCALL_CLOUD_CLIENT_COUNTRY_HEADER` (`CF-IPCountry` behind Cloudflare) feeds the country.
- Beta plans: one per account, given with the admin CLI or redeemed from an invite code
  (`POST /v1/entitlements/redeem`).
- Admin CLI: `wristcall-cloud admin show`, `grant`, `revoke`, `invites create` and `stats`, with exit codes 0 to 3.
- Limits against abuse: silent accounts per client address (3 an hour, 10 a day, IPv6 by /64) and in total (200 a day,
  a warning above 100), attempts per minute on the routes that take a secret, 10 devices and 5 open enroll codes per
  account, and one poll a second per web session. All of them are configurable. `POST /v1/session/refresh` and polls
  of web sessions with an unknown id are left to the reverse proxy.
- Cleanup: an anonymous account (no identity, no plan) unused for 90 days is deleted, and so is a device unused for that
  long.
- `WRISTCALL_CLOUD_MONGO_URL_FILE`: the MongoDB URL can come from a file (a Docker secret), like the other secrets.

### Changed

- `GET /v1/account` also answers `plan`, `identities` and `key_set`. `GET /v1/config` also answers `sessions`
  (`enabled`, `zitadel_max_age_s`, `legacy_zitadel_tokens`). `DELETE /v1/account` deletes the devices, identities and
  plan too, and the Zitadel users of all the account's identities; it needs an app or the web app.
- The `client_id` of a per-server token asked with an account session comes from the device platform (iOS client for
  `ios`, `ipados` and `macos`, watch client for `watchos`, PWA client for `web`), so servers that list `clients` keep
  accepting the tokens.

### Deprecated

- The Zitadel token as the credential for account routes. From 0.4.0 it is only taken in the exchange
  (`POST /v1/devices`) and as the key reset proof (`POST /v1/account/key/rotate`), if issued less than 10 minutes
  ago, and, while `WRISTCALL_CLOUD_LEGACY_ZITADEL_TOKENS=1`, on the routes that existed in 0.3.x. Turn the flag off once the v3 apps of the iPhone and the PWA are published.

### Upgrade notes

- On every start the Cloud creates the Zitadel identity of each account from 0.3.x that has none (no account is
  changed; idempotent; the log says how many) and sets `meta.schema` to 2. Going back to 0.3.1 needs no restore: it
  ignores the new collections and fields.
- Set `WRISTCALL_CLOUD_LEGACY_ZITADEL_TOKENS=1` while PWA 0.1.0 and the iPhone app `ios-v0.1.0` are in use; they send a
  Zitadel token on the old routes and get `401` without it. Turn it off once the v3 apps ship.
- Behind a proxy set `WRISTCALL_CLOUD_CLIENT_IP_HEADER` (the account limits are per client address), and behind
  Cloudflare `WRISTCALL_CLOUD_CLIENT_COUNTRY_HEADER=CF-IPCountry`.
- With `WRISTCALL_CLOUD_MONGO_URL_FILE` do not also set `WRISTCALL_CLOUD_MONGO_URL` (configuration error). The admin CLI
  then works with `docker exec <container> wristcall-cloud admin ...` and no extra variables.

## [cloud-0.3.1] - 2026-10-15

### Changed

- `/v1/config` also lists the organization scope (`urn:zitadel:iam:org:id:<WRISTCALL_CLOUD_ZITADEL_ORG_ID>`) when the
  organization is set, so the Zitadel login shows the wristcall branding and texts. The apps already sign in with the
  Cloud's scopes, so they need no update.

## [web-0.1.0] - 2026-10-11

First release of the web app (`web/`, tag `web-v0.1.0`, image `ghcr.io/ggondim/wristcall-web`). It is meant for servers
0.7.0 (older ones send no CORS headers) and, for the account features, Cloud 0.3.0.

### Added

- A PWA (TypeScript, Vite, Preact) that manages wristcall servers from a browser: servers (personal token or pairing
  code), agents, devices and requests to approve, pairing codes and the call history, like the iPhone app.
- Central account (when the site is given a Cloud, `WRISTCALL_WEB_CLOUD_URL`): sign in with PKCE, mirror servers to
  the account, approve the watch's device code sign-in, sign out and delete the account.
- Web Push device approval notifications through the Cloud's relay (installed on the Home Screen on iOS 16.4 or later).
  A tap opens the Approvals screen with the request highlighted; the notification has no buttons.
- Offline app shell (service worker), strict Content Security Policy (no inline script, nothing from a CDN) and an
  nginx image without root (port 8080) that writes `/config.json` from `WRISTCALL_WEB_CLOUD_URL`.
- End to end tests with Playwright against a local server, a local Cloud and the real identity provider
  (`npm run e2e`).

### Known limitations

- App icons are placeholders (the repository has no app icon yet).
- The watch sign-in code accepts a space in the middle (the watch app does not).
- Web Push on iOS needs the site installed on the Home Screen; a server on plain `http` on the local network does not
  work from an `https` site. See [web/README.md](web/README.md).

## [cloud-0.3.0] - 2026-10-11

### Added

- Account deletion deletes the sign-in too (Apple's account deletion rule): `DELETE /v1/account` also deletes the
  caller's user at Zitadel, through the management API as a machine user that manages one organization
  (`WRISTCALL_CLOUD_ZITADEL_ORG_ID`, `_CLIENT_ID`, `_CLIENT_SECRET` or `_FILE`; all or none). A user of another
  organization is left alone. If Zitadel fails, the agenda is already deleted and the call answers
  `503 account_unavailable`; the app shows the error and a retry finishes. Without the variables only the agenda is
  deleted, as before.
- CORS for listed browser origins (`WRISTCALL_CLOUD_CORS_ORIGINS`, a comma separated list of exact origins; empty:
  no CORS, as before), so the wristcall PWA can call the API. Methods `GET`, `POST`, `PUT`, `PATCH`, `DELETE`; request
  headers `Authorization` and `Content-Type`; no credentials; `Retry-After` is exposed. See "Browsers (CORS)" in
  [cloud/README.md](cloud/README.md).

### Changed

- iPhone app (next release): the "Delete account" confirmation says the sign-in is deleted too.
- The end-to-end browser helper (`tools/e2e/zitadel_browser.py`) drives Zitadel's login v2: the iPhone sign-in
  (PKCE) is now verified against the real identity provider by the account e2e.

## [0.7.0] - 2026-10-11

### Added

- CORS for listed browser origins: `server.cors_origins` in `wristcall.yaml` (exact origins, `https` or `http` on
  loopback; empty by default: no CORS, as before), so the wristcall PWA can call the management API. Methods `GET`,
  `POST`, `PUT`, `PATCH`, `DELETE`; request headers `Authorization` and `Content-Type`; no credentials;
  `Content-Disposition` is exposed (the export's file name). A server on plain `http` outside localhost cannot be
  used from a PWA served over `https` (mixed content). See "Browsers (CORS)" in [docs/protocol.md](docs/protocol.md).

## [ios-0.1.0] - 2026-10-10

First release of the iPhone app (`watch/Phone/`, tag `ios-v0.1.0`). It is meant for servers 0.6.0 (older ones lack parts of the
management API).

Notes: it ships inside bundle version 0.4.0 (build 4), the same as the watch app, because Apple requires the embedded
watch app's version to match the iPhone app's. No binary is published. Push notifications (APNs), App Store
distribution and Sign in with Apple need a paid Apple Developer account; everything else builds with a free Apple ID.

### Added

- Servers: add one with its address and a personal token (a pasted device token or a wrong one saves nothing), rename,
  remove. Tokens live in the iPhone Keychain.
- Agents: list, create, edit, delete and reorder, for conversation, one-shot and monologue agents.
- Devices: paired watches, pending pairing requests to approve or deny (read on opening, on coming to the foreground and
  every 10 seconds on the devices screen), pairing codes, and "Add to watch" over WatchConnectivity to the embedded
  watch app.
- History: the calls of every server in one list, with search, filters, export and redelivery of failed one-way calls.
- Central account (when the build has a Cloud, `WRISTCALL_CLOUD_URL`): sign in with PKCE, add a server with a pairing
  code, link a server, mirror servers and agents to the account, approve the watch's device code login, sign out and
  delete the account.
- Push build (`DebugPush`): device approval notifications with Approve and Deny actions.
- UI smoke test with screenshots (`make -C watch test-ios-ui`, its own scheme, not part of CI).

### Known limitations

- The iPhone sign-in (PKCE) could not be verified against the real identity provider: its hosted login page (login v2)
  was down when this was built (verified after release, see cloud-0.3.0). The watch's device code sign-in was
  verified against the real identity provider by a Kit integration test, not through the watch or iPhone screens.
- "Delete account" deletes only the agenda in the Cloud (with Cloud 0.3.0 also the sign-in, see there) (push registrations at the relay are anonymous and not part of
  the account). The app then drops its own push keys (push build), but only until its next launch: on the next launch it
  registers the keys again for every server that accepts the account; removing the server in the app clears its key
  for good.
- The default build has `WRISTCALL_CLOUD_URL` empty, so the account features are hidden until the Cloud is deployed.

## [watch-0.4.0] - 2026-10-10

Works with the same servers as 0.3.0. The account sign-in needs a Cloud in the build and servers 0.6.0.

### Watch app

- Sign in with the central account through a device code, approved on the iPhone; the watch then pairs the account's
  servers, asking for approval on the iPhone when the server requires it ("Sync with account", "Sign out of account").
- WatchConnectivity pairing: the iPhone app sends a server address and a pairing code, and asks the watch to refresh
  its agents.

### Changed

- The bundle id is now `<BUNDLE_ID_PREFIX>.wristcall.watchkitapp` (the iPhone app owns `<BUNDLE_ID_PREFIX>.wristcall`),
  and the app has the companion keys (`WKCompanionAppBundleIdentifier`, `WKRunsIndependentlyOfCompanionApp`) instead of
  `WKWatchOnly`. The widgets extension is `<BUNDLE_ID_PREFIX>.wristcall.watchkitapp.widgets`. The watch app installs
  through the iPhone app.

### Upgrade notes

- Delete the 0.3.0 watch app from the watch first: the new bundle id is a different app with its own Keychain, so it
  has to be paired again. A Personal Team registers three new App IDs (iPhone app, watch app, widgets extension).
- The Cloud's `WRISTCALL_CLOUD_APNS_TOPICS` must list `io.github.ggondim.wristcall` and
  `io.github.ggondim.wristcall.watchkitapp` (with your own prefix if you set one) for push to reach both apps.

## [watch-0.3.0] - 2026-10-10

Works with the same servers as 0.2.0. Push notifications need the push build (below), a server 0.6.0 with `push`
configured and a relay (wristcall Cloud 0.2.0); with servers 0.5.0 and older the watch has no push and polls as before.

### Watch app

- A one-way call without a final status is remembered (call id, server and agent ids, time; never the text) for 24
  hours: the next time the app opens it asks the server again, so closing the app no longer loses the result.
- Push notifications in the `DebugPush` build configuration only (`WRISTCALL_PUSH`, `Config/Push.xcconfig`,
  `make -C watch build-sim-push`). Debug and Release are as in 0.2.0 and never ask for notifications.
- The relay comes from the app's own configuration (`WRISTCALL_RELAY_URL`, `WRISTCALL_PUSH_ENVIRONMENT`), never from a
  server: a server whose `/v1/health` names another relay, or none, is skipped (URLs compared with scheme and host
  lowercased, default port and trailing slash dropped).
- One relay key per paired server: the watch registers its APNs token at the relay (label: the server's host, up to 64 characters; tag: the
  server's local id; event `call.finished`), hands the key to the server (`PUT /v1/push`) and checks every key each
  time the app comes to the foreground (a key the relay forgot is registered again, a new APNs token replaces the old
  keys). Removing a server clears its key on the server, then at the relay.
- A `call.finished` notification for the result on screen ends its wait at once; any other one shows a banner, and
  tapping it opens that call's result. Permission is asked when the first one-way result shows, not at launch.
- Debug launch argument `-WCFakeAPNsToken <hex>`: the watch simulator gets no APNs token.
- `WristcallKit`: push relay client, push payloads and the pending result store.

### Upgrade notes

- The push build needs the `aps-environment` entitlement, which a free Apple ID (Personal Team) cannot sign: on a real
  watch it needs a paid Apple Developer account. Without one, install the Debug build as before (no push); the push
  build runs in the simulator.

## [cloud-0.2.0] - 2026-10-10

First published release of the wristcall Cloud (`cloud/`, image `ghcr.io/ggondim/wristcall-cloud`, from the
`cloud-v*` tag). It brings the account and agenda of epic E5 (0.1.0, never deployed) and the per-server tokens and
push relay of epic E6. Configuration, routes and deployment notes: [cloud/README.md](cloud/README.md).

### Added

- Central account (OIDC, `WRISTCALL_CLOUD_ISSUER`, app client ids): `GET /v1/account`, `DELETE /v1/account` (iOS and
  PWA clients only), the user's servers (`/v1/servers`) and their agent lists (`PUT /v1/servers/{id}/agents`,
  `GET /v1/agents`), in MongoDB. Public `GET /v1/config` tells apps how to sign in.
- Account tokens: `aud` may be an app client id or, with `WRISTCALL_CLOUD_PROJECT_ID`, the project id (what Zitadel
  puts in app tokens asked with the project audience scope); `client_id`/`azp` must always be an app client id.
- Per-server tokens: `POST /v1/server-tokens {"audience"}` turns a central account login into an ES256 token
  (`typ` `wc-server+jwt`, 300 seconds) made for one server URL, signed with `WRISTCALL_CLOUD_SIGNING_KEY` under
  `WRISTCALL_CLOUD_PUBLIC_URL`; `/.well-known/openid-configuration` and `/v1/jwks` publish the key.
- Push relay: anonymous `POST /v1/push/registrations` returns a `wc_push_` key; `POST /v1/push/send`,
  `GET` and `DELETE /v1/push/registrations/current` take it. Channels: APNs (HTTP/2, provider token from the team's
  `.p8` key) and Web Push (RFC 8291 `aes128gcm`, RFC 8292 VAPID), each only when its credentials are set.
- The registration's `label` (notification subtitle) and `tag` (`wristcall.tag`) are forced on every message of its
  key; a registration lists the `events` it accepts (`call.finished`, `device.approval`; `test` always).
- Limits: 30 sends a minute and 500 a day per key, 10 registrations a minute per client address, 20 registrations per
  APNs token or Web Push endpoint; registrations idle for 180 days are deleted. All adjustable by environment.
- `GET /v1/config` adds `server_tokens` and `push` (channels, VAPID public key, APNs topics).
- `WRISTCALL_CLOUD_PUSH_FAKE` (loopback only): a channel that delivers nothing, for local end to end tests.
- Image workflow (`cloud-image`), multi-arch, on `cloud-v*` tags.

### Security

- The Cloud API refuses per-server tokens and servers 0.6.0 refuse the central account's own token, so neither can be
  replayed at the other or at another server. This lifts the block on deploying the Cloud noted in 0.5.0 only once
  every server linked to a Cloud account runs 0.6.0 with `central_account.audience`. A server still on 0.5.0 with a
  `central_account` receives the user's login token, and its operator can exchange it at `POST /v1/server-tokens` for
  valid per-server tokens for any 0.6.0 server where that user is linked (and pair a device there in `attestation`
  mode, or send the user approval requests). Upgrade every 0.5.0 server with `central_account` (or unlink its users)
  before setting `WRISTCALL_CLOUD_SIGNING_KEY`; apps must never send login tokens to servers.
- APNs answers `BadDeviceToken` and `DeviceTokenNotForTopic` delete the registration and log a warning (reason only):
  many of them at once point to a wrong environment or topic in the Cloud's configuration.
- Only the SHA-256 of a push key is stored. Web Push endpoints must be https on port 443 at a known push service
  (`WRISTCALL_CLOUD_WEBPUSH_HOSTS`); redirects are never followed. A database error is `503`, never the `410` that makes
  a server drop a key.
- Tokens, push keys, device tokens, endpoints, titles, bodies and labels never reach the log; configuration errors
  name the variable, never its value.
- Privacy: the title, body and label of a notification (the label is the server's host for the watch) pass through
  the Cloud and then Apple or the browser's push service. The transcript of a call never does.

### Deploy notes

- Needs MongoDB, the signing key and, for Web Push, the VAPID key (generate them with `openssl`, see the README) as
  secrets. The APNs channel needs a paid Apple Developer account (the `.p8` key); without it, run with Web Push only.
- The registration route is anonymous: also limit it at the reverse proxy and set `WRISTCALL_CLOUD_CLIENT_IP_HEADER`.

## [0.6.0] - 2026-10-10

### Added

- Push through the wristcall Cloud relay (optional `push` section in `wristcall.yaml`: `relay_url`, and `timeout_s`,
  10 by default, up to 30). Without it the server behaves as 0.5.0 and the push routes answer `404 not_configured`.
- `PUT /v1/push {"push_key"}` (device or API token) keeps the client's relay key, one per paired device or API token:
  a new key replaces the old one, which the server drops at the relay. `DELETE /v1/push` removes it.
- When a one-shot or monologue call reaches its final status, the device that made it gets `call.finished` (title and
  body with the outcome and the agent's name; `data` `{"call_id","status","error","agent_id"}`; one notification per
  call). When a device asks to pair with a central account login in `approval` mode, the user's management apps (keys
  set with an API token) get `device.approval` (`data` `{"request_id","device_name","expires_at"}`).
- Push is best effort and never holds anything up: the call and the pairing request are answered first, a relay that
  is down, slow or refusing only shows in the log (status or error type, never the key), and a stopping server waits at
  most 8 s for it. A key the relay no longer knows (`410`) is forgotten.
- `GET /v1/health` adds `push` (`{"relay"}` or `null`).

### Changed

- **Breaking for `central_account`:** the server accepts only per-server tokens signed by the wristcall Cloud
  (`typ` `wc-server+jwt`, `aud` = this server's URL). `central_account.issuer` is now the Cloud URL and the new
  `central_account.audience` (this server's URL(s), as apps reach it) is required (both are kept in canonical form:
  scheme and host lowercased, default port and trailing slash dropped): a 0.5.0 `central_account`
  without it stops the server at startup (`central_account.audience: Field required`). `clients` is optional.
  The account key of a link becomes `<Cloud URL>#<account issuer>#<subject>`. Without `central_account` nothing changes.
- Reference client: `pair-account` takes `--cloud`, refuses a server whose `account.issuer` is another Cloud, and asks
  the Cloud for a token made for the server URL as typed.

### Security

- Per-server tokens close the replay noted in 0.5.0: a token is made for one server URL and lives 5 minutes; a server
  refuses tokens made for another server and the central account's own login token, and the Cloud API refuses
  per-server tokens. Apps ask for the token for the URL they connect to, never one a server announces, and know the
  Cloud and relay URLs from their own configuration.
- Push keys never reach the log or a response. Notifications carry only a title and a body with the outcome and the
  agent's name: the transcript of a call never leaves the server through push. The title, body and label (the
  server's host, set by the device) pass through the Cloud and Apple or the browser's push service.

### Upgrade notes

- Back up `data_dir` first. The database migrates to schema 6 on start (the new `push_targets` table). Rolling back to
  0.5.0 needs that backup: 0.5.0 refuses the newer schema, and setting `user_version` back by hand makes the next
  upgrade fail, because step 6 runs again on a database that has the table.
- A server with an E5 (0.5.0) `central_account` does not start until it is updated: point `issuer` at the Cloud URL
  and add `audience`. Links made with 0.5.0 named the account issuer and no longer match: users link again (from the
  app, with an API token or a pairing code). Servers without `central_account` (as `account: null` in `/v1/health`)
  need nothing.
- Watches talking to servers 0.5.0 and older get no push; they poll the call result as before.
- The APNs path (watch and iPhone notifications) needs the Cloud's APNs channel, which needs a paid Apple Developer
  account; until then the watch's push build stays a simulator build.

## [watch-0.2.0] - 2026-10-09

Needs server 0.3.0 or later for agents and turn modes and 0.4.0 or later for one-way agents. With 0.2.x servers it works as one conversation agent per profile.

### Watch app

- Several servers: pair more than one (Settings > Servers > Add server), remove them one by one. A watch paired with 0.1.0 keeps its server (the Keychain item is migrated). A server that is down does not stop the others, and a revoked one is removed on its own.
- Agent grid: Home shows the agents of every server with their icons; tap to call, long press or the "…" in the toolbar for the call options. Messages show above the grid.
- One-way agents (one-shot, monologue): "Recording" screen with "Send", then a progress ring, a check or the failure reason, and the transcribed text. The watch polls `GET /v1/calls/{id}` while the app is open, for up to 3 minutes ("Check again" after that); push comes later.
- "Call <agent>" complication, control and shortcut for a chosen agent; with no agent chosen yet the new complication and control only open the app, and one whose agent was deleted says "Agent not found." The 0.1.0 ones still call the first agent, which is now the first agent of the first server: with that server down the watch says so and calls nobody. Redialing from the system's call history calls the agent with that name only when the name is unique.
- The new items read the agent list from an App Group, which is required to install: Xcode must register it on the first build for your watch, or signing fails (fallback without the App Group: the new complication and control still appear in the gallery but find no agents to pick, while the 0.1.0 ones keep working; see [watch/README.md](watch/README.md)).
- "Send" on a one-way call before it connects says "Nothing was sent."; a malformed agent in `GET /v1/me` is skipped instead of making the whole server unreachable.
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
