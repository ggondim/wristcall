# wristcall cloud

The central account API: lets a user sign in with the central account (OIDC, `auth.trigram.com.br`) and keep the
list of their wristcall servers in one place. It is a separate service from `server/`, with its own package
(`wristcall-cloud`), its own MongoDB database and its own release cycle.

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

Configuration errors name the variable, never its value.

## Authentication

Requests carry the central account access token: `Authorization: Bearer <token>`. The token is verified locally
against the issuer's published keys (signature, issuer, time claims); its audience and the client it was issued
to must be one of the configured client ids. The account is identified as `<issuer>#<subject>`. A missing or bad
token is `401 unauthorized`; the issuer being unreachable with no cached keys is `503 account_unavailable`.

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

## Rate limiting

The service does not limit request rates itself: the reverse proxy of the deployment (Traefik) is expected to, per client
address, in front of every route.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
WRISTCALL_CLOUD_TEST_MONGO_URL=mongodb://localhost:27017 .venv/bin/python -m pytest -q
docker build -t wristcall-cloud .
```

Tests need a MongoDB (`WRISTCALL_CLOUD_TEST_MONGO_URL`, default `mongodb://localhost:27017`); each test uses a
throwaway `test_<uuid>` database and drops it.
