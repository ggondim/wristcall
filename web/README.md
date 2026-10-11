# wristcall web app (PWA)

A browser app to manage your wristcall servers from any device: add a server, create and edit agents, approve the
watches that wait to pair, make pairing codes and read the call history. It is the web counterpart of the iPhone
app. It does not make calls; the watch does.

It is a static site (TypeScript, Vite, Preact) that talks to your servers and, optionally, to the wristcall Cloud
straight from the browser. There is no backend of its own: the site stores your server list and credentials in the
browser and sends them only to the servers you added. Version 0.1.0, published as the image
`ghcr.io/ggondim/wristcall-web` by the `web-vX.Y.Z` tag. It is meant for servers 0.7.0 (older ones send no CORS
headers, so a browser cannot call them) and, for the account features, Cloud 0.3.0.

What it does:

- Servers: add one with its address and a personal token (`wristcall users tokens add --name web`), or with a pairing
  code when you are signed in; rename, remove.
- Agents: list, create, edit and delete.
- Devices: paired watches, requests to approve or deny, pairing codes.
- History: the calls of every server in one list, with search, filters, export and redelivery.
- With a Cloud: sign in with the central account, mirror the server list to the account, approve a watch sign-in by
  its code, delete the account, and get a notification when a watch asks to sign in (Web Push).

## Run it

```sh
docker run -p 8080:8080 -e WRISTCALL_WEB_CLOUD_URL=https://cloud.example.com ghcr.io/ggondim/wristcall-web:0.1.0
```

The image is nginx without root and serves the site on port 8080. Put TLS in front of it: a PWA, its service worker and
Web Push need `https` (only `localhost` is exempt). `WRISTCALL_WEB_CLOUD_URL` is the address of your wristcall Cloud:
empty (the default) hides everything that needs an account, `https://host[:port]`, `http://localhost[:port]` or `http://127.0.0.1[:port]` for development. Anything else stops the container at start. The container writes the address to `/config.json`, which the
page reads when it opens, so one image fits any deployment. The Cloud address is the site operator's decision: not the
user's and not a server's.

## What the operator sets up

For the site to work with your servers and Cloud, these must agree (replace `https://app.example.com` with the site's
public address, the origin of the page):

| Where | Setting | Value |
|---|---|---|
| Each wristcall server | `server.cors_origins` in `wristcall.yaml` | `[https://app.example.com]`, or the server refuses every call from the browser |
| The Cloud | `WRISTCALL_CLOUD_CORS_ORIGINS` | `https://app.example.com` |
| The Cloud | `WRISTCALL_CLOUD_CLIENT_PWA` | the OIDC client id of the PWA (without it there is no sign-in from the site, and `DELETE /v1/account` answers `403`) |
| Zitadel, application `wristcall-pwa` (PKCE, no secret) | redirect URI | `https://app.example.com/auth/callback` |
| Zitadel, same application | post logout URI | `https://app.example.com/` |
| The site's container | `WRISTCALL_WEB_CLOUD_URL` | the Cloud's canonical public URL, the same value as the Cloud's `WRISTCALL_CLOUD_PUBLIC_URL` (same scheme, host and port, no trailing slash). A server that names a different Cloud as its account issuer or push relay is refused |

The server and the Cloud send no CORS headers by default, so nothing opens by itself. See "Browsers (CORS)" in
[docs/protocol.md](../docs/protocol.md) and in [cloud/README.md](../cloud/README.md). For the watch to sign in through
the app, a server needs `central_account` with `device_credential: approval` and the PWA's client id in `clients`.
Web Push also needs the Cloud's VAPID key (`WRISTCALL_CLOUD_VAPID_PRIVATE_KEY`) and `push.relay_url` on the server.

## Differences from the iPhone app

- "Add watch" shows the 8 digit code and the server address to type on the watch. There is no WatchConnectivity in a
  browser, so nothing is handed over.
- "Approve watch sign-in": you type the code the watch shows and the app opens the provider's approval page for it
  (`{issuer}/device?user_code=XXXX-XXXX`, the issuer comes from the Cloud). The code is accepted with or without the
  hyphen and with a space in the middle (`ABCD EFGH`), unlike the watch app (`WristcallKit`), which does not take a
  space in the middle. The space is accepted on purpose: it is how people read a code aloud.
- A device approval notification has no Approve and Deny buttons: web notifications cannot show them on iOS, and on
  other systems they would approve from the lock screen without any unlock. A tap opens the Approvals screen with that
  request highlighted; approving is done there, after a confirmation.
- The app icons (`public/icons/`) are placeholders drawn for this release. The repository has no app icon yet; replace
  the four PNG files (192, 512, maskable 512 and the 180 Apple touch icon) when it has.

## Limitations

- Web Push on iOS and iPadOS (16.4 or later) works only for the site installed on the Home Screen (Share, then Add to
  Home Screen) and opened from there. In a Safari tab the app explains it and asks for nothing. The permission prompt
  always comes from a tap on "Turn on notifications".
- A server on plain `http://` on your local network does not work from a site served over `https`: browsers block
  mixed content. Put the server behind TLS (a reverse proxy with a real certificate). `http://localhost`,
  `http://127.0.0.1` and `http://[::1]` are the exception.
- Chrome 142 and later (Local Network Access): a public site that calls a server on a private address asks for the
  permission for access to devices on the local network. The user must allow it once. A server with a
  public name behind TLS asks nothing.
- Credentials (personal tokens, the account's refresh token) live in the browser's storage (IndexedDB), on this
  device. Anyone who can use the browser profile can use them, and a script that runs in the page could read them: the
  site ships a strict Content Security Policy (no inline script) for that reason, and loads nothing from a CDN. The access token of the account session stays in memory. After the
  first server is saved the app asks the browser for persistent storage (`navigator.storage.persist()`), so the data
  is not evicted when the device is short of space; a browser may refuse, and then the data can still be erased.
- On iOS the Safari tab and the installed app have separate storage: servers added in the tab do not appear in the
  installed app (install first, then add your servers: the installed app is the one to rely on). In a Safari tab, Intelligent Tracking Prevention erases the
  storage of a site not used for 7 days; the installed app is exempt. With an account, "From your account" lists the
  servers of the agenda that have no token on this device, so adding them again is one step.
- The service worker keeps only the app's own files, so the site opens offline, but every screen needs the server.
  It never touches a server, the Cloud or `/config.json`.

## Develop

Node 24.

```sh
npm ci
npm run dev        # http://localhost:5173 (no service worker, so no Web Push)
npm run typecheck
npm test           # Vitest
npm run build      # typecheck, site, then the service worker
npm run check:dist # checks the build works under the CSP: no inline script or style, a classic service worker
npm run preview    # serves the build on 5173 (the service worker runs here)
```

Port 5173 is fixed on purpose: it is the redirect address registered for the sign-in in development.

To try it with a Cloud, set `cloudUrl` in `public/config.json` (or in `dist/config.json` after a build). Without a
Cloud (`"cloudUrl": ""`) the app manages servers with personal tokens only.

## End to end tests

`npm run e2e` runs Playwright (full Chromium, new headless mode; `npx playwright install chromium` once) against the
built site served by `npm run preview`, a local server and a local Cloud, with three specs in a fixed order, one
worker:

1. `servers.spec.ts`: a site without a Cloud and a personal token: add a server, agents, pairing code, history, remove.
2. `push.spec.ts`: sign in, link the server with a pairing code, turn on notifications with a fixed Web Push
   subscription (headless Chromium has no push service), register at the Cloud's relay and at the server, make a watch
   ask to sign in, see the Cloud's fake push channel accept it, deliver a push message to the service worker through
   the DevTools protocol, check the notification and open the approvals link it points at.
3. `account.spec.ts`: sign in, link, agenda, approve a watch sign-in request, sign out at the provider, sign in again,
   delete the account. It runs last because it deletes the account.

The two account specs sign in at the real identity provider as a test user, so they need that setup. The variables
(`WRISTCALL_E2E_SERVER`, `_CLOUD`, `_PAT`, `_USER`, `_CLI`, `_CONFIG`, `_CLOUD_LOG`, `_ZITADEL_USER`,
`_ZITADEL_PASSWORD_FILE`) are read in `e2e/env.ts`; a spec whose variables are missing skips with the reason.

The Cloud of a local run needs: `WRISTCALL_CLOUD_CLIENT_PWA` (the PWA's client id; without it the verifier refuses the
PWA's token and `DELETE /v1/account` answers `403`), `WRISTCALL_CLOUD_ALLOW_LOOPBACK_AUDIENCE=1` (so per-server tokens
can name `http://127.0.0.1:8765`), `WRISTCALL_CLOUD_PUBLIC_URL` on loopback (`http://127.0.0.1:8090`) with a signing key
(`WRISTCALL_CLOUD_SIGNING_KEY_FILE`), `WRISTCALL_CLOUD_PUSH_FAKE=1`, a VAPID key
(`WRISTCALL_CLOUD_VAPID_PRIVATE_KEY_FILE` and `_SUBJECT`), its log at INFO level (the push spec reads it, through
`WRISTCALL_E2E_CLOUD_LOG`) and `WRISTCALL_CLOUD_CORS_ORIGINS=http://localhost:5173`. The server needs
`server.cors_origins: [http://localhost:5173]`, a `central_account` that names the local Cloud, and `push.relay_url`.
The built `dist/config.json` must point to the local Cloud (`{"cloudUrl": "http://127.0.0.1:8090"}`). The maintainers'
runner script that sets all of this up is not in this repository. The three specs are chained projects: if the push spec
fails, the account spec is skipped. The specs never print tokens, passwords or push keys, and the test config records
no trace, screenshot or video, because the sign-in page would end up in them.

## Image

```sh
docker build -t wristcall-web .
./scripts/smoke-image.sh wristcall-web   # the checks CI runs: routes, types, headers, config.json
```
