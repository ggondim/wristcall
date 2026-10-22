"""An in-process fake of the Wristcall Cloud's node API (`/v1/node/*`, `cloud/README.md` "Cloud nodes") for the
cloud-mode tests: same routes, status codes, error codes and JSON shapes as `cloud/src/wristcall_cloud/node_*.py`,
with the state in public dicts a test reads and changes.

Simplified on purpose: one node and one month; no orphan sweeper or heartbeat bookkeeping; grants are opaque
(`grant-<call>-<n>`); any well-formed push key is a registration and a push to a device with a target is sent.
It also serves the Cloud's discovery document and JWKS (issuer `https://cloud.test`), so the server's
`OidcVerifier` checks `user_token()` tokens for real.
"""

import hashlib
import json
import math
import re
import secrets
import time
import unicodedata
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from oidc_fixtures import ec_key, public_jwk
from starlette.exceptions import HTTPException as StarletteHTTPException

CLOUD_URL = "https://cloud.test"
FULL_PLAN = {"plan": "full", "source": "beta", "expires_at": None}
KID = "fake-cloud"
TOKEN_TYPE = "wc-server+jwt"
PLATFORMS = ("ios", "ipados", "macos", "watchos", "web")
MANAGERS = ("ios", "ipados", "macos", "web")
CALL_TYPES = ("conversation", "one-shot", "monologue")
ONE_WAY = ("one-shot", "monologue")
STATUSES = ("recording", "processing", "delivered", "failed", "empty", "ended")
FINAL = ("delivered", "failed", "empty", "ended")
COLORS = ("red", "orange", "yellow", "green", "mint", "teal", "blue", "indigo", "purple", "pink")
NODE_MAX_BODY = 1024 * 1024
GRANT_SLACK_S, EXPIRED_GRANT_WINDOW_S, LEEWAY_S = 3600, 86400, 60
AGENT_ID = re.compile(r"^ag_[0-9a-f]{12}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ICON = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)*$")
TERM_HASH = re.compile(r"^x[0-9a-f]{16}$")
ERROR_CODE = re.compile(r"^[a-z_]{1,32}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
PUSH_KEY = re.compile(r"^wc_push_[A-Za-z0-9_-]{43}$")
AGENT_FIELDS = ("id", "slug", "display_name", "icon", "color", "call_type", "position", "spec", "created_at", "updated_at")
CALL_FIELDS = (
    "id", "agent_id", "agent_slug", "agent_name", "device_id", "call_type", "status", "error", "attempts",
    "last_http_status", "delivery", "created_at", "updated_at", "ended_at", "finished_at", "expires_at", "seconds",
    "open",
)
ENTRY_FIELDS = ("seq", "role", "text", "sealed", "error", "at")
CALL_PATCH_FIELDS = {"status", "error", "attempts", "last_http_status", "ended_at", "finished_at", "delivery"}
MESSAGE_FIELDS = {"event", "title", "body", "data", "ttl_s", "collapse_id"}


class CloudError(Exception):
    def __init__(self, status: int, code: str, message: str = "request failed", retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


def _invalid(message: str = "the request is not valid") -> CloudError:
    return CloudError(422, "invalid", message)


def _not_found(what: str = "call") -> CloudError:
    return CloudError(404, "not_found", f"{what} not found")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _integer(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _error_code(value: Any) -> str | None:
    if value is not None and not (isinstance(value, str) and ERROR_CODE.fullmatch(value)):
        raise _invalid("error must be null or a short code")
    return value


def _delivery(value: Any) -> str | None:
    if value not in (None, "webhook", "history"):
        raise _invalid("delivery must be null, webhook or history")
    return value


def _agent_fields(body: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "id": lambda v: isinstance(v, str) and AGENT_ID.fullmatch(v),
        "slug": lambda v: isinstance(v, str) and SLUG.fullmatch(v),
        "display_name": lambda v: isinstance(v, str) and 0 < len(v) <= 64
        and not any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in v),
        "icon": lambda v: isinstance(v, str) and len(v) <= 64 and ICON.fullmatch(v),
        "color": lambda v: v is None or v in COLORS,
        "call_type": lambda v: v in CALL_TYPES,
        "spec": lambda v: isinstance(v, dict)
        and len(json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode()) <= 256 * 1024,
    }
    for name, value in body.items():
        if not checks[name](value):
            raise _invalid(f"{name} is not valid")
    days = body.get("spec", {}).get("retention_days")
    if days not in (None, "forever") and not _integer(days, 1, 36500):
        raise _invalid("spec.retention_days is not valid")
    return dict(body)


def _entry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid("entries must be objects")
    seq, text, sealed, hashes = value.get("seq"), value.get("text"), value.get("sealed", False), value.get("term_hashes") or []
    if not _integer(seq, 0, 100000) or value.get("role") not in ("user", "agent") or not _number(value.get("at")):
        raise _invalid("entry is not valid")
    if not isinstance(sealed, bool) or (text is not None and (not isinstance(text, str) or not sealed)):
        raise _invalid("text must be sealed")
    if not isinstance(hashes, list) or len(hashes) > 2000 or not all(isinstance(h, str) and TERM_HASH.fullmatch(h) for h in hashes):
        raise _invalid("term_hashes are not valid")
    return {"seq": seq, "role": value["role"], "text": text, "sealed": sealed, "error": _error_code(value.get("error")),
            "at": value["at"], "term_hashes": list(dict.fromkeys(hashes))}


def _call_patch(body: dict[str, Any]) -> dict[str, Any]:
    if not body or not set(body) <= CALL_PATCH_FIELDS:
        raise _invalid("patch fields not allowed")
    if "status" in body and body["status"] not in STATUSES:
        raise _invalid("status is not valid")
    if "error" in body:
        _error_code(body["error"])
    if "attempts" in body and not _integer(body["attempts"], 0, 10000):
        raise _invalid("attempts is not valid")
    if "last_http_status" in body and body["last_http_status"] is not None and not _integer(body["last_http_status"], 100, 599):
        raise _invalid("last_http_status is not valid")
    if "delivery" in body:
        _delivery(body["delivery"])
    for name in ("ended_at", "finished_at"):
        if name in body and body[name] is not None and not _number(body[name]):
            raise _invalid(f"{name} is not valid")
    return dict(body)


def _counts(value: Any, nullable: bool) -> bool:
    return isinstance(value, dict) and all(
        k in value and (_integer(value[k], 0, 1_000_000) or (nullable and value[k] is None)) for k in ("conversation", "one_way")
    )


def _heartbeat(body: dict[str, Any]) -> None:
    active = body.get("active_call_ids")
    texts = [(body.get("boot_id"), 64), (body.get("version"), 32)] + [(c, 64) for c in active or []]
    if (
        not isinstance(active, list) or len(active) > 500 or not isinstance(body.get("providers_ok"), bool)
        or not all(isinstance(t, str) and 0 < len(t) <= n for t, n in texts)
        or not _counts(body.get("active_by_type"), False) or not _counts(body.get("capacity"), True)
    ):
        raise _invalid("heartbeat is not valid")


def _message(body: dict[str, Any]) -> None:
    title, text = body.get("title"), body.get("body", "")
    if (
        not set(body) <= MESSAGE_FIELDS or not isinstance(body.get("event"), str)
        or not (isinstance(title, str) and 1 <= len(title) <= 100) or not (isinstance(text, str) and len(text) <= 300)
        or not isinstance(body.get("data", {}), dict)
    ):
        raise _invalid("message is not valid")


def _pick(doc: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: doc.get(name) for name in fields}


class FakeCloud:
    """The Cloud for one node (`node_url`, `node_secret`). State (all public, change it freely in a test):
    `plans` account -> plan object or None (missing: None); `revoked` device ids; `quota_s` monthly quota and
    `used_s` account -> seconds used; `agents` id -> stored agent (public fields plus `account`); `calls` id -> stored
    call (public fields plus `account`, `platform`, `request_id`, `max_s`); `entries` call id -> seq -> entry (plus
    `term_hashes`); `grants` grant -> `{call, account, device, platform, exp}`; `push_targets` "account:device" ->
    push key; `pushes` what `POST /v1/node/push` sent (`{account, device, call, message}`); `heartbeats` bodies
    received; `requests` every request received (`seed_call` adds a call to the history); `down` (every route 503);
    `retry_after` (every node route 429 with that Retry-After); `max_agents`, `max_conversation_s`, `max_one_way_s`,
    `retention_days`."""

    def __init__(self, node_url: str, node_secret: str) -> None:
        self.node_url, self.node_secret = node_url, node_secret
        self.key = ec_key()
        self.plans: dict[str, dict | None] = {}
        self.revoked: set[str] = set()
        self.quota_s = 600 * 60
        self.used_s: dict[str, int] = {}
        self.agents: dict[str, dict] = {}
        self.calls: dict[str, dict] = {}
        self.entries: dict[str, dict[int, dict]] = {}
        self.grants: dict[str, dict] = {}
        self.push_targets: dict[str, str] = {}
        self.pushes: list[dict] = []
        self.heartbeats: list[dict] = []
        self.requests: list[dict] = []  # every request received: method, path, params (pairs), headers (lowercase)
        self.down = False
        self.retry_after: int | None = None
        self.max_agents = 20
        self.max_conversation_s = 3600
        self.max_one_way_s = 1800
        self.retention_days = 90
        self.app = self._build()

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=CLOUD_URL)

    def user_token(
        self, account: str | None, device: str | None, platform: str | None, *, aud: str | None = None,
        typ: str = TOKEN_TYPE, **claims: Any,
    ) -> str:
        """A per-server token as the Cloud signs for this node; a claim given as None is left out."""
        now = int(time.time())
        body = {"iss": CLOUD_URL, "sub": account, "aud": aud or self.node_url, "did": device, "plt": platform,
                "iat": now, "exp": now + 300, "jti": secrets.token_hex(16), **claims}
        body = {k: v for k, v in body.items() if v is not None}
        return jwt.encode(body, self.key, algorithm="ES256", headers={"kid": KID, "typ": typ})

    # --- what the routes share -------------------------------------------------------------------------------

    def usage(self, account: str) -> dict[str, Any]:
        used = self.used_s.get(account, 0)
        month = datetime.now(UTC).strftime("%Y-%m")
        return {"month": month, "used_s": used, "quota_s": self.quota_s, "remaining_s": max(0, self.quota_s - used)}

    def seed_call(
        self, account: str, *, agent_id: str = "ag_000000000000", call_type: str = "one-shot",
        status: str = "delivered", created_at: float | None = None, open: bool = False, device_id: str = "dev-1",
        entries: list[dict[str, Any]] | None = None, **fields: Any,
    ) -> str:
        """A call already in the history (as nodes would have written it), with its entries (`seq`, `role`, `text`,
        `sealed`, `error`, `at`, `term_hashes`; missing ones default). Returns its id."""
        now = time.time() if created_at is None else created_at
        call = {
            "id": "c_" + secrets.token_hex(8), "agent_id": agent_id, "agent_slug": "agent", "agent_name": "Agent",
            "device_id": device_id, "call_type": call_type, "status": "recording" if open else status, "error": None,
            "attempts": 0, "last_http_status": None, "delivery": "history" if call_type in ONE_WAY else None,
            "created_at": now, "updated_at": now, "ended_at": None if open else now,
            "finished_at": None if open else now, "expires_at": now + self.retention_days * 86400,
            "seconds": None if open else 1, "open": open, "account": account, "platform": "ios", "request_id": None,
            "max_s": self.max_one_way_s, **fields,
        }
        self.calls[call["id"]] = call
        for e in entries or []:
            entry = {"role": "user", "text": None, "sealed": False, "error": None, "at": now, "term_hashes": [], **e}
            self.entries.setdefault(call["id"], {})[entry["seq"]] = entry
        return call["id"]

    def _days(self, spec: dict[str, Any] | None) -> int:
        days = (spec or {}).get("retention_days")
        return self.retention_days if days is None else 365 if days == "forever" else min(days, 365)

    def _agents_of(self, account: str) -> list[dict[str, Any]]:
        mine = [a for a in self.agents.values() if a["account"] == account]
        return [_pick(a, AGENT_FIELDS) for a in sorted(mine, key=lambda a: (a["position"], a["created_at"]))]

    def _listing(self, account: str) -> JSONResponse:
        agents = self._agents_of(account)
        etag = _etag(agents)
        return JSONResponse({"agents": agents, "etag": etag}, headers={"ETag": etag})

    def _agent(self, account: str, ref: str) -> dict[str, Any] | None:
        return next((a for a in self.agents.values() if a["account"] == account and ref in (a["id"], a["slug"])), None)

    def _node(self, request: Request) -> None:
        if request.headers.get("authorization") != f"Bearer {self.node_secret}":
            raise CloudError(401, "unauthorized", "missing or invalid node credential")
        if self.retry_after is not None:
            raise CloudError(429, "rate_limited", "too many requests; try again later", self.retry_after)

    def _user(self, request: Request, token: str | None = None) -> tuple[str, str, str]:
        """(account, device, platform) of the user token, checked as the Cloud does."""
        self._node(request)
        token = token or request.headers.get("x-wristcall-user-token") or ""
        try:
            if jwt.get_unverified_header(token).get("typ") != TOKEN_TYPE:
                raise CloudError(401, "unauthorized")
            claims = jwt.decode(
                token, self.key.public_key(), algorithms=["ES256"], audience=self.node_url, issuer=CLOUD_URL,
                leeway=LEEWAY_S, options={"require": ["exp", "iat", "iss", "aud", "sub", "did", "plt"]},
            )
        except jwt.PyJWTError:
            raise CloudError(401, "unauthorized", "missing or invalid credential") from None
        account, device, platform = claims["sub"], claims["did"], claims["plt"]
        if claims["aud"] != self.node_url or not (account and device) or platform not in PLATFORMS:
            raise CloudError(401, "unauthorized", "missing or invalid credential")
        if device in self.revoked:
            raise CloudError(403, "device_revoked", "this device was removed from the account")
        return account, device, platform

    def _manager(self, request: Request, *, plan: bool = True) -> tuple[str, str, str]:
        """The user token of an app or the web (a watch gets 403 `forbidden`), with a plan unless `plan=False`."""
        account, device, platform = self._user(request)
        if platform not in MANAGERS:
            raise CloudError(403, "forbidden", "this device cannot do this")
        if plan and self.plans.get(account) is None:
            raise CloudError(402, "plan_required", "this needs an active plan")
        return account, device, platform

    def _grant(self, request: Request, call_id: str, *, allow_expired: bool) -> dict[str, Any]:
        self._node(request)
        grant = self.grants.get(request.headers.get("x-wristcall-call-grant") or "")
        now = time.time()
        if grant is None or grant["call"] != call_id:
            raise CloudError(401, "unauthorized", "missing or invalid credential")
        if grant["exp"] < now and (not allow_expired or grant["exp"] + EXPIRED_GRANT_WINDOW_S < now):
            raise CloudError(401, "unauthorized", "call grant expired")
        return grant

    def _issue(self, call: dict[str, Any], device: str, platform: str, max_s: int) -> str:
        grant = f"grant-{call['id']}-{len(self.grants) + 1}"
        self.grants[grant] = {"call": call["id"], "account": call["account"], "device": device, "platform": platform,
                              "exp": time.time() + max_s + GRANT_SLACK_S}
        return grant

    def _granted_call(self, request: Request, call_id: str) -> dict[str, Any]:
        grant = self._grant(request, call_id, allow_expired=True)
        call = self.calls.get(call_id)
        if call is None or call["account"] != grant["account"]:
            raise _not_found()
        return call

    def _call_entries(self, call_id: str) -> list[dict[str, Any]]:
        return [_pick(e, ENTRY_FIELDS) for _, e in sorted(self.entries.get(call_id, {}).items())]

    def _query_calls(self, request: Request, account: str) -> list[dict[str, Any]]:
        """The account's calls in the `agent`/`since`/`until` window, oldest first."""
        q = request.query_params
        try:
            since, until = (float(q[k]) if k in q else None for k in ("since", "until"))
        except ValueError:
            raise _invalid("since and until must be numbers") from None
        calls = [
            c for c in self.calls.values()
            if c["account"] == account and q.get("agent") in (None, c["agent_id"])
            and (since is None or c["created_at"] >= since) and (until is None or c["created_at"] < until)
        ]
        return sorted(calls, key=lambda c: (c["created_at"], c["id"]))

    # --- the routes ------------------------------------------------------------------------------------------

    def _build(self) -> FastAPI:  # noqa: C901 - one closure per route keeps the fake in one place
        app = FastAPI()

        @app.middleware("http")
        async def gate(request: Request, call_next):
            self.requests.append({"method": request.method, "path": request.url.path,
                                  "params": request.query_params.multi_items(), "headers": dict(request.headers)})
            if self.down:
                return JSONResponse({"error": "unavailable", "message": "the Cloud is down"}, status_code=503)
            length = request.headers.get("content-length")
            if request.url.path.startswith("/v1/node/") and length and int(length) > NODE_MAX_BODY:
                return JSONResponse({"error": "too_large", "message": "body too large"}, status_code=413)
            return await call_next(request)

        @app.exception_handler(CloudError)
        async def cloud_error(_request: Request, e: CloudError) -> JSONResponse:
            headers = {"Retry-After": str(e.retry_after)} if e.retry_after is not None else None
            return JSONResponse({"error": e.code, "message": e.message}, status_code=e.status, headers=headers)

        @app.exception_handler(StarletteHTTPException)
        async def http_error(_request: Request, e: StarletteHTTPException) -> JSONResponse:
            code = {404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "http_error")
            return JSONResponse({"error": code, "message": "request failed"}, status_code=e.status_code)

        async def body_of(request: Request) -> dict[str, Any]:
            try:
                body = json.loads(await request.body())
            except (ValueError, RecursionError):
                raise _invalid("body must be valid JSON") from None
            if not isinstance(body, dict):
                raise _invalid("body must be a JSON object")
            return body

        @app.get("/.well-known/openid-configuration")
        async def discovery() -> dict[str, Any]:
            return {"issuer": CLOUD_URL, "jwks_uri": f"{CLOUD_URL}/v1/jwks",
                    "id_token_signing_alg_values_supported": ["ES256"], "response_types_supported": []}

        @app.get("/v1/jwks")
        async def jwks() -> dict[str, Any]:
            return {"keys": [public_jwk(self.key, KID, "ES256")]}

        @app.get("/v1/node/me")
        async def me(request: Request) -> dict[str, Any]:
            account, device, platform = self._user(request)
            return {"account": account, "plan": self.plans.get(account), "device": {"id": device, "platform": platform},
                    "agents": self._agents_of(account), "usage": self.usage(account)}

        @app.get("/v1/node/agents")
        async def list_agents(request: Request) -> Response:
            account, _, _ = self._user(request)
            etag = _etag(self._agents_of(account))
            match = request.headers.get("if-none-match")
            if match and any(p.strip().removeprefix("W/") in (etag, "*") for p in match.split(",")):
                return Response(status_code=304, headers={"ETag": etag})
            return self._listing(account)

        @app.put("/v1/node/agents/order")
        async def order_agents(request: Request) -> Response:
            account, _, _ = self._manager(request)
            body = await body_of(request)
            ref, index = body.get("agent"), body.get("index")
            if set(body) != {"agent", "index"} or not isinstance(ref, str) or not _integer(index, -(2**63), 2**63):
                raise _invalid("send agent and index")
            ids = [a["id"] for a in self._agents_of(account)]
            if ref not in ids:
                raise _not_found("agent")
            ids.remove(ref)
            ids.insert(min(max(index, 0), len(ids)), ref)
            for position, agent_id in enumerate(ids):
                self.agents[agent_id]["position"] = position
            return self._listing(account)

        @app.get("/v1/node/agents/{ref}")
        async def get_agent(ref: str, request: Request) -> dict[str, Any]:
            account, _, _ = self._user(request)
            agent = self._agent(account, ref)
            if agent is None:
                raise _not_found("agent")
            return _pick(agent, AGENT_FIELDS)

        @app.post("/v1/node/agents", status_code=201)
        async def create_agent(request: Request) -> dict[str, Any]:
            account, _, _ = self._manager(request)
            body = await body_of(request)
            required = {"id", "slug", "display_name", "icon", "call_type", "spec"}
            if set(body) - required - {"color"} or required - set(body):
                raise _invalid("agent fields not allowed or missing")
            fields = {"color": None, **_agent_fields(body)}
            mine = [a for a in self.agents.values() if a["account"] == account]
            if len(mine) >= self.max_agents:
                raise CloudError(403, "limit", f"at most {self.max_agents} agents per account")
            if fields["id"] in self.agents or any(a["slug"] == fields["slug"] for a in mine):
                raise CloudError(409, "conflict", "an agent with this id or slug exists")
            now = time.time()
            position = max((a["position"] for a in mine), default=-1) + 1
            self.agents[fields["id"]] = {**fields, "account": account, "position": position, "created_at": now,
                                         "updated_at": now}
            return _pick(self.agents[fields["id"]], AGENT_FIELDS)

        @app.patch("/v1/node/agents/{agent_id}")
        async def patch_agent(agent_id: str, request: Request) -> dict[str, Any]:
            account, _, _ = self._manager(request)
            body = await body_of(request)
            if not body or set(body) - {"slug", "display_name", "icon", "color", "call_type", "spec"}:
                raise _invalid("patch fields not allowed")
            fields = _agent_fields(body)
            agent = self.agents.get(agent_id)
            if agent is None or agent["account"] != account:
                raise _not_found("agent")
            if "slug" in fields and any(
                a["slug"] == fields["slug"] and a["account"] == account and a["id"] != agent_id for a in self.agents.values()
            ):
                raise CloudError(409, "conflict", "an agent with this slug exists")
            agent.update(fields, updated_at=time.time())
            if "spec" in fields:  # the Cloud moves the expiry of the agent's calls with `retention_days`
                for call in self.calls.values():
                    if call["agent_id"] == agent_id:
                        call["expires_at"] = call["created_at"] + self._days(fields["spec"]) * 86400
            return _pick(agent, AGENT_FIELDS)

        @app.delete("/v1/node/agents/{agent_id}", status_code=204)
        async def delete_agent(agent_id: str, request: Request) -> Response:
            account, _, _ = self._manager(request, plan=False)
            if self.agents.get(agent_id, {}).get("account") != account:
                raise _not_found("agent")
            del self.agents[agent_id]
            return Response(status_code=204)

        @app.post("/v1/node/calls", status_code=201)
        async def open_call(request: Request, response: Response) -> dict[str, Any]:
            account, device, platform = self._user(request)
            body = await body_of(request)
            ref, call_type, request_id = body.get("agent"), body.get("call_type"), body.get("request_id")
            if not isinstance(ref, str) or not 0 < len(ref) <= 64 or call_type not in CALL_TYPES:
                raise _invalid("agent or call_type is not valid")
            if request_id is not None and not (isinstance(request_id, str) and REQUEST_ID.fullmatch(request_id)):
                raise _invalid("request_id is not valid")
            delivery = _delivery(body.get("delivery"))
            plan = self.plans.get(account)
            if plan is None:
                raise CloudError(402, "plan_required", "this needs an active plan")

            def answer(call: dict[str, Any], agent: dict[str, Any]) -> dict[str, Any]:
                return {"call_id": call["id"], "grant": self._issue(call, device, platform, call["max_s"]),
                        "agent": _pick(agent, AGENT_FIELDS), "plan": plan,
                        "quota_s": self.usage(account)["remaining_s"], "max_s": call["max_s"],
                        "started_at": call["created_at"]}

            replay = next((c for c in self.calls.values() if request_id is not None and c["open"]
                           and c["account"] == account and c["device_id"] == device and c["request_id"] == request_id), None)
            if replay is not None and (agent := self.agents.get(replay["agent_id"])) is not None:
                if replay["call_type"] != call_type or ref not in (agent["id"], agent["slug"]):
                    raise CloudError(409, "conflict", "request_id was used to open a different call")
                response.status_code = 200
                return answer(replay, agent)
            agent = self._agent(account, ref)
            if agent is None:
                raise _not_found("agent")
            if agent["call_type"] != call_type:
                raise _invalid("call_type does not match the agent")
            remaining = self.usage(account)["remaining_s"]
            if remaining <= 0:
                raise CloudError(402, "quota_exceeded", "this month's minutes are used up")
            if any(c["open"] and c["account"] == account for c in self.calls.values()):
                raise CloudError(409, "too_many_calls", "this account already has a call open")
            now = time.time()
            days = self._days(agent["spec"])
            call = {
                "id": "c_" + secrets.token_hex(8), "agent_id": agent["id"], "agent_slug": agent["slug"],
                "agent_name": agent["display_name"], "device_id": device, "call_type": call_type, "status": "recording",
                "error": None, "attempts": 0, "last_http_status": None, "delivery": delivery, "created_at": now,
                "updated_at": now, "ended_at": None, "finished_at": None, "expires_at": now + days * 86400,
                "seconds": None, "open": True, "account": account, "platform": platform, "request_id": request_id,
                "max_s": min(self.max_one_way_s if call_type in ONE_WAY else self.max_conversation_s, remaining),
            }
            self.calls[call["id"]] = call
            return answer(call, agent)

        @app.post("/v1/node/calls/{call_id}/entries", status_code=204)
        async def add_entries(call_id: str, request: Request) -> Response:
            self._granted_call(request, call_id)
            entries = (await body_of(request)).get("entries")
            if not isinstance(entries, list) or len(entries) > 100:
                raise _invalid("entries must be a list of at most 100 entries")
            parsed = [_entry(e) for e in entries]
            if len({e["seq"] for e in parsed}) != len(parsed):
                raise _invalid("entries must not repeat a seq")
            for e in parsed:
                self.entries.setdefault(call_id, {}).setdefault(e["seq"], e)
            return Response(status_code=204)

        @app.patch("/v1/node/calls/{call_id}", status_code=204)
        async def patch_call(call_id: str, request: Request) -> Response:
            call = self._granted_call(request, call_id)
            call.update(_call_patch(await body_of(request)), updated_at=time.time())
            return Response(status_code=204)

        @app.post("/v1/node/calls/{call_id}/finish", status_code=204)
        async def finish_call(call_id: str, request: Request) -> Response:
            call = self._granted_call(request, call_id)
            body = await body_of(request)
            status, seconds = body.get("status"), body.get("seconds")
            if status not in STATUSES or status == "recording" or not _number(seconds):
                raise _invalid("status or seconds is not valid")
            error = _error_code(body.get("error"))
            if call["open"]:  # the first finish counts; a repeated one changes nothing
                now = time.time()
                seconds = math.ceil(min(max(seconds, 0), call["max_s"] + 60))
                call.update(status=status, error=error, seconds=seconds, ended_at=now, updated_at=now, open=False,
                            finished_at=now if status in FINAL else call["finished_at"])
                self.used_s[call["account"]] = self.used_s.get(call["account"], 0) + seconds
            return Response(status_code=204)

        @app.get("/v1/node/calls")
        async def list_calls(request: Request) -> dict[str, Any]:
            account, _, _ = self._manager(request, plan=False)
            q = request.query_params
            limit = q.get("limit", "50")
            if not limit.isdigit() or not 1 <= int(limit) <= 200:
                raise _invalid("limit must be an integer from 1 to 200")
            if ("before_at" in q) != ("before_id" in q):
                raise _invalid("before_at and before_id go together")
            groups = [raw.split(",") for raw in q.getlist("terms")]
            if len(groups) > 16 or any(len(g) > 16 or not all(TERM_HASH.fullmatch(h) for h in g) for g in groups):
                raise _invalid("terms are not valid")
            calls = self._query_calls(request, account)[::-1]
            if "before_at" in q:
                cursor = (float(q["before_at"]), q["before_id"])
                calls = [c for c in calls if (c["created_at"], c["id"]) < cursor]

            def matches(call: dict[str, Any]) -> bool:
                entries = self.entries.get(call["id"], {}).values()
                return all(any(set(g) & set(e["term_hashes"]) for e in entries) for g in groups)

            return {"calls": [_pick(c, CALL_FIELDS) for c in calls if matches(c)][: int(limit)]}

        @app.get("/v1/node/calls/export")
        async def export_calls(request: Request) -> StreamingResponse:
            account, _, _ = self._manager(request, plan=False)
            calls = self._query_calls(request, account)

            async def lines():
                for call in calls:
                    line = {"call": _pick(call, CALL_FIELDS), "entries": self._call_entries(call["id"])}
                    yield json.dumps(line) + "\n"
                yield json.dumps({"end": True, "count": len(calls)}) + "\n"

            return StreamingResponse(lines(), media_type="application/x-ndjson")

        @app.get("/v1/node/calls/{call_id}")
        async def read_call(call_id: str, request: Request) -> dict[str, Any]:
            account, device, platform = self._user(request)
            call = self.calls.get(call_id)
            if call is None or call["account"] != account or (platform == "watchos" and call["device_id"] != device):
                raise _not_found()
            return {"call": _pick(call, CALL_FIELDS), "entries": self._call_entries(call_id)}

        @app.delete("/v1/node/calls/{call_id}", status_code=204)
        async def delete_call(call_id: str, request: Request) -> Response:
            account, _, _ = self._manager(request, plan=False)
            call = self.calls.get(call_id)
            if call is None or call["account"] != account:
                raise _not_found()
            if call["open"]:
                raise CloudError(409, "call_open", "this call is still open")
            del self.calls[call_id]
            self.entries.pop(call_id, None)
            return Response(status_code=204)

        @app.delete("/v1/node/calls")
        async def delete_calls(request: Request) -> dict[str, int]:
            account, _, _ = self._manager(request, plan=False)
            agent, everything = request.query_params.get("agent"), request.query_params.get("all") == "true"
            if agent is None and not everything:
                raise _invalid("give agent or all=true")
            doomed = [c["id"] for c in self.calls.values() if c["account"] == account and not c["open"]
                      and agent in (None, c["agent_id"])]
            for call_id in doomed:
                del self.calls[call_id]
                self.entries.pop(call_id, None)
            return {"deleted": len(doomed)}

        @app.post("/v1/node/calls/{call_id}/redeliver")
        async def redeliver_call(call_id: str, request: Request) -> dict[str, Any]:
            account, device, platform = self._manager(request, plan=False)
            call = self.calls.get(call_id)
            if call is None or call["account"] != account:
                raise _not_found()
            if call["call_type"] not in ONE_WAY:
                raise CloudError(409, "not_one_way", "only one-shot and monologue calls are delivered")
            if call["open"]:
                raise CloudError(409, "call_open", "this call is still open")
            if call["status"] == "processing":
                raise CloudError(409, "busy", "this call is being delivered right now")
            if call["status"] != "failed" or call["error"] not in ("delivery_failed", "interrupted"):
                raise CloudError(409, "not_failed", "only a call whose delivery failed can be delivered again")
            call.update(status="processing", error=None, finished_at=None, updated_at=time.time())
            agent = self.agents.get(call["agent_id"])
            return {"call": _pick(call, CALL_FIELDS), "entries": self._call_entries(call_id),
                    "agent": _pick(agent, AGENT_FIELDS) if agent else None,
                    "grant": self._issue(call, device, platform, self.max_one_way_s)}

        @app.put("/v1/node/push-targets", status_code=204)
        async def put_push_target(request: Request) -> Response:
            account, device, _ = self._user(request)
            body = await body_of(request)
            if set(body) != {"push_key"} or not isinstance(body["push_key"], str) or not PUSH_KEY.fullmatch(body["push_key"]):
                raise _invalid("body must be an object with a push_key")
            self.push_targets[f"{account}:{device}"] = body["push_key"]
            return Response(status_code=204)

        @app.delete("/v1/node/push-targets", status_code=204)
        async def delete_push_target(request: Request) -> Response:
            account, device, _ = self._user(request)
            self.push_targets.pop(f"{account}:{device}", None)
            return Response(status_code=204)

        @app.post("/v1/node/push", status_code=202)
        async def push(request: Request) -> dict[str, int]:
            self._node(request)
            body = await body_of(request)
            call = body.pop("call", None)
            if request.headers.get("x-wristcall-call-grant") is not None:
                if not isinstance(call, str) or not call:
                    raise _invalid("call must be the id of the call")
                grant = self._grant(request, call, allow_expired=False)
                account, device = grant["account"], grant["device"]
            else:
                account, device, _ = self._user(request)
            _message(body)
            if f"{account}:{device}" not in self.push_targets:
                return {"sent": 0}
            self.pushes.append({"account": account, "device": device, "call": call, "message": body})
            return {"sent": 1}

        @app.post("/v1/node/heartbeat", status_code=204)
        async def heartbeat(request: Request) -> Response:
            self._node(request)
            body = await body_of(request)
            _heartbeat(body)
            self.heartbeats.append(body)
            return Response(status_code=204)

        return app


def _etag(agents: list[dict[str, Any]]) -> str:
    keys = [[a["id"], a["updated_at"], a["position"]] for a in agents]
    return '"' + hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode()).hexdigest()[:32] + '"'
