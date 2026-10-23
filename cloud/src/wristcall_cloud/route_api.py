"""`POST /v1/route`: the node that serves a call (`purpose: call`) or the management of the account's cloud-mode
agents and history (`purpose: manage`), with a per-server token bound to that node and, when there is one, a fallback
node with its own token.

The node list is read from Mongo on every request (a handful of documents; the admin CLI writes it from another
process). Failure reports (`avoid`) live in `app.state.node_reports`. Logs name the node id and the purpose only:
never a node URL nor a token. Call routes are counted in the hourly summary (`node_stats`, issued or refused for want
of a node); a failed count changes no answer.
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, Header, Request

from .account_store import AccountStore
from .api_common import ApiError, json_object
from .auth import Caller, current_device
from .cloud_store import CloudModeStore
from .config import CloudConfig, provision_settings
from .node_stats import record_route
from .nodes import NodeStore, parse_node_id
from .provision import cloud_entry, provision, voice_notes_language
from .routing import NodeReports, RouteNeed, call_kind, choose_nodes
from .sessions_api import client_country
from .signing import SigningKey, server_token_claims
from .store import Store

log = logging.getLogger("wristcall_cloud.route")

router = APIRouter()

PURPOSES = ("call", "manage")
MAX_AVOID = 8
MAX_AGENT_ID = 64
_FIELDS = frozenset({"purpose", "agent", "avoid"})


def _invalid(message: str) -> ApiError:
    return ApiError("invalid", message, 422)


def _signing_key(request: Request) -> SigningKey:
    # The rule of app.signing_key, repeated: app.py imports this module to include its router.
    key: SigningKey | None = request.app.state.signing_key
    if key is None:
        raise ApiError("not_configured", "this cloud does not issue server tokens", 404)
    return key


def parse_route(body: dict[str, Any]) -> tuple[str, str | None, frozenset[str]]:
    """(purpose, agent id for a call, node ids to avoid) of a route request; 422 `invalid` otherwise. Messages never
    echo the values."""
    if not set(body) <= _FIELDS:
        raise _invalid("unknown field; allowed: purpose, agent, avoid")
    purpose = body.get("purpose")
    if purpose not in PURPOSES:
        raise _invalid("purpose must be call or manage")
    agent = body.get("agent")
    if purpose == "call":
        if not isinstance(agent, str) or not agent or len(agent) > MAX_AGENT_ID:
            raise _invalid("agent must be the id of one of the account's agents")
    elif "agent" in body:
        raise _invalid("agent is only for purpose call")
    avoid = body.get("avoid", [])
    if not isinstance(avoid, list) or len(avoid) > MAX_AVOID:
        raise _invalid(f"avoid must be a list of at most {MAX_AVOID} node ids")
    try:
        ids = frozenset(parse_node_id(node_id) for node_id in avoid)
    except ValueError:
        raise _invalid("avoid must hold node ids") from None
    return purpose, agent, ids


def _issue(
    key: SigningKey, config: CloudConfig, node: dict[str, Any], caller: Caller, device: dict[str, Any], now: float
) -> dict[str, Any]:
    """The node as the response shows it, with a token bound to its URL that names the stored device's platform."""
    claims = server_token_claims(
        issuer=config.public_url,
        account=caller.key,
        client_id=caller.client_id,
        audience=node["url"],
        now=now,
        ttl_s=config.server_token_ttl_s,
        device=device["_id"],
        platform=device["platform"],
    )
    return {
        "node": {"id": node["_id"], "url": node["url"], "region": node["region"]},
        "token": key.sign(claims),
        "expires_at": claims["exp"],
    }


def _plan_required() -> ApiError:
    return ApiError("plan_required", "this account has no plan for the wristcall Cloud", 402)


@router.post("/v1/route")
async def issue_route(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    key = _signing_key(request)
    caller = await current_device(request, authorization)
    purpose, agent_id, avoid = parse_route(await json_object(request))
    state = request.app.state
    config: CloudConfig = state.config
    accounts: AccountStore = state.accounts
    store: Store = state.store
    cloud: CloudModeStore = state.cloud
    now = time.time()

    # The token carries the platform of the stored device; gone since authentication (a race): revoked.
    device = await accounts.device(caller.key, caller.device) if caller.device is not None else None
    if device is None:
        raise ApiError("device_revoked", "this device was removed from the account", 403)

    entitlement = await accounts.entitlement(caller.key, now=now)
    plan = None if entitlement is None else entitlement["plan"]
    country = client_country(request, config.client_country_header)
    if plan is None:
        # A lapsed plan still manages (the history stays readable), never calls.
        if purpose == "call" or await cloud_entry(store, caller.key) is None:
            raise _plan_required()
    elif await cloud_entry(store, caller.key) is None:
        # Provisioning after a redeem may have failed: the first route makes up for it.
        settings = provision_settings(config)
        if settings is not None:
            language = voice_notes_language(country, settings.language)
            await provision(store, cloud, caller.key, plan, settings, language=language, now=now)

    kind = None
    if purpose == "call":
        agent = await cloud.agents.find_one({"_id": agent_id, "account": caller.key}, {"call_type": 1})
        if agent is None:
            raise ApiError("not_found", "agent not found", 404)
        kind = call_kind(agent["call_type"])

    nodes = await NodeStore(cloud).list()
    reports: NodeReports = state.node_reports
    known = {node["_id"] for node in nodes}
    for node_id in avoid & known:  # unknown ids never make an entry
        reports.record(node_id, caller.key, now)

    need = RouteNeed(plan=plan, kind=kind, country=country, avoid=avoid)
    chosen = choose_nodes(nodes, need, config=config, reports=reports, started_at=state.started_at, now=now)
    if not chosen:
        log.info("no node available (purpose %s)", purpose)
        if purpose == "call":
            await record_route(cloud.node_stats, now, no_node=True)
        raise ApiError(
            "no_node_available",
            "no wristcall Cloud node can take this now; try again later",
            503,
            retry_after=config.route_retry_after_s,
        )
    response = _issue(key, config, chosen[0], caller, device, now)
    response["fallback"] = _issue(key, config, chosen[1], caller, device, now) if len(chosen) > 1 else None
    log.info("route issued (node %s, purpose %s)", chosen[0]["_id"], purpose)
    if purpose == "call":
        await record_route(cloud.node_stats, now, no_node=False)
    return response
