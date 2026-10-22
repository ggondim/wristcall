"""The cloud-mode node (`server.mode: cloud`), assembled once by `create_app`: the Cloud client, the storage adapter,
the authenticator, the outbox and the heartbeat. Like a travelling cashier: the outbox is the drawer where receipts
wait while the head office is unreachable, and the heartbeat is the "we are open" sent to it every few seconds.

The node takes calls only after the Cloud accepted its first heartbeat (`ready`) and while it is not draining.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import httpx

from .. import __version__
from ..admission import CallSlots
from ..agents import AgentError, AgentSpec, ProviderRef
from ..config import AppConfig
from ..oidc import OidcVerifier
from ..storage.cloud import CloudStorage
from ..storage.models import AgentRecord, CloudUnavailable, PlanRequired
from .auth import CloudAuthenticator
from .client import CloudClient, raise_for_account
from .heartbeat import Heartbeat
from .outbox import CallRegistry, Outbox
from .proof import UserProof, require_proof
from .seal import SealCodec

log = logging.getLogger("wristcall.cloud_node.node")

SERVER_TOKEN_TYPE = "wc-server+jwt"
PLAN_TTL_S = 30.0
PLAN_CACHE_SIZE = 4096
NEEDS_VOICE_URL = "{field}: this plan needs your own endpoint."


class CloudNode:
    def __init__(
        self,
        *,
        client: CloudClient,
        storage: CloudStorage,
        auth: CloudAuthenticator,
        registry: CallRegistry,
        outbox: Outbox,
        heartbeat: Heartbeat,
        seal: SealCodec,
        clock: Callable[[], float] = time.monotonic,
        plan_ttl_s: float = PLAN_TTL_S,
    ) -> None:
        self.client = client
        self.storage = storage
        self.auth = auth
        self.registry = registry
        self.outbox = outbox
        self.heartbeat = heartbeat
        self.seal = seal
        self.ready = asyncio.Event()  # set at the first heartbeat the Cloud accepts
        self.draining = False  # set when the node stops taking calls before exiting (Task 8b)
        self._clock = clock
        self._plan_ttl_s = plan_ttl_s
        self._plans: OrderedDict[str, tuple[float, str]] = OrderedDict()  # account -> (fetched at, plan)
        self._beating: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return f"CloudNode(ready={self.ready.is_set()}, draining={self.draining})"

    @classmethod
    def build(
        cls,
        config: AppConfig,
        *,
        slots: CallSlots,
        http: httpx.AsyncClient,
        outbox_max_delay_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> "CloudNode":
        """`http` reaches the Cloud (its `/v1/node/*` API and its signing keys); the caller owns and closes it."""
        node_config, central = config.cloud_node, config.central_account
        if not config.is_cloud or node_config is None or central is None:
            raise ValueError("a cloud node needs server.mode: cloud with cloud_node and central_account")
        client = CloudClient(node_config.cloud_url, node_config.node_secret.get_secret_value(), http)
        seal = SealCodec(node_config.seal())
        registry = CallRegistry()
        verifier = OidcVerifier(central.issuer, list(central.audience), http, typ=SERVER_TOKEN_TYPE)
        return cls(
            client=client,
            storage=CloudStorage(client, seal),
            auth=CloudAuthenticator(verifier),
            registry=registry,
            outbox=Outbox(client, registry, max_delay_s=outbox_max_delay_s),
            heartbeat=Heartbeat(
                client, registry, slots, config.limits.max_concurrent_calls,
                version=__version__, every_s=node_config.heartbeat_s,
            ),
            seal=seal,
            clock=clock,
        )

    @property
    def accepting(self) -> bool:
        """Takes new calls: the Cloud accepted a heartbeat and the node is not draining."""
        return self.ready.is_set() and not self.draining

    async def start(self) -> None:
        """Starts the outbox worker and the heartbeat (in the running loop)."""
        self.outbox.start()
        if self._beating is None or self._beating.done():
            self._beating = asyncio.create_task(self.heartbeat.run(self.ready))

    async def drain_calls(self, slots: CallSlots, timeout_s: float = 30.0, *, poll_s: float = 0.1) -> int:
        """Stops taking calls (`accepting` False: health 503 `draining`, a new call `busy`) and waits up to `timeout_s`
        for the open ones (`slots`) to end; returns how many are still open. The Cloud hears it at once: one heartbeat
        offering no capacity, so it routes no new call here (best effort; the regular beats offer none either)."""
        self.draining = True
        self.heartbeat.draining = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        telling = asyncio.create_task(self.heartbeat.beat())
        try:
            still_open = sum(slots.active().values())
            if still_open:
                log.info("draining: waiting up to %g s for %d open call(s)", timeout_s, still_open)
            while still_open and loop.time() < deadline:
                await asyncio.sleep(max(0.0, min(poll_s, deadline - loop.time())))
                still_open = sum(slots.active().values())
        finally:
            if not telling.done():
                telling.cancel()
            await asyncio.gather(telling, return_exceptions=True)
        if still_open:
            log.warning("draining: %d call(s) still open after %g s; leaving anyway", still_open, timeout_s)
        else:
            log.info("draining: no open calls")
        return still_open

    async def stop(self, drain_s: float = 20.0) -> int:
        """Stops the heartbeat, gives the outbox up to `drain_s` to empty, and returns how many items were lost."""
        beating, self._beating = self._beating, None
        if beating is not None:
            beating.cancel()
            with suppress(asyncio.CancelledError):
                await beating
        left = await self.outbox.drain(drain_s)
        await self.storage.close()
        return left

    async def me(self, proof: UserProof) -> dict[str, Any]:
        """`GET /v1/node/me` for the proof's user: `{account, plan, device, agents, usage}`, with `agents` opened
        (`AgentRecord`, sealed endpoints opened with the proof's account; one that does not open is left out).
        Raises `PlanRequired`, `DeviceRevoked` or `CloudUnavailable` (also for an answer without its fields)."""
        resp = await self.client.request("GET", "/v1/node/me", user=proof)
        raise_for_account(resp)
        if resp.status != 200:
            log.warning("Wristcall Cloud answered %d (%s) on GET /v1/node/me", resp.status, resp.code)
            raise CloudUnavailable(f"Wristcall Cloud answered {resp.status} on GET /v1/node/me")
        body = resp.body
        try:
            plan, usage, raws = body["plan"], body["usage"], body["agents"]
            if plan is not None and not (isinstance(plan, dict) and isinstance(plan["plan"], str)):
                raise TypeError("plan")
            if not isinstance(usage, dict) or not isinstance(raws, list):
                raise TypeError("usage or agents")
        except (KeyError, TypeError):
            log.warning("Wristcall Cloud answered GET /v1/node/me without the expected fields")
            raise CloudUnavailable("Wristcall Cloud answered GET /v1/node/me without the expected fields") from None
        agents: list[AgentRecord] = self.storage.agents.opened(proof.account, raws)
        self._remember(proof.account, plan["plan"] if plan else None)
        return {"account": proof.account, "plan": plan, "device": body.get("device"), "agents": agents, "usage": usage}

    async def plan(self, proof: UserProof) -> str | None:
        """The proof's account's plan (`full`, `custom`...), None without one. A plan is kept 30 s per account; "no
        plan" is not kept, so a plan bought a moment ago counts at once."""
        cached = self._plans.get(proof.account)
        if cached is not None and self._clock() - cached[0] < self._plan_ttl_s:
            return cached[1]
        return _plan_name(await self.me(proof))

    def _remember(self, account: str, plan: str | None) -> None:
        self._plans.pop(account, None)
        if plan is None:
            return
        if len(self._plans) >= PLAN_CACHE_SIZE:
            self._plans.popitem(last=False)  # the account fetched longest ago
        self._plans[account] = (self._clock(), plan)

    async def check_agent_plan(self, user_id: str, spec: AgentSpec) -> None:
        """`AgentService`'s plan check, before an agent is written: no plan raises `PlanRequired`; the `custom` plan
        (the user's own voice endpoints) refuses an operator's `stt` or `tts` (`AgentError` `needs_voice_url`)."""
        plan = await self.plan(require_proof(user_id))
        if plan is None:
            raise PlanRequired("this account has no Wristcall Cloud plan")
        if plan == "custom":
            for field in ("stt", "tts"):
                if isinstance(getattr(spec, field), ProviderRef):
                    raise AgentError("needs_voice_url", NEEDS_VOICE_URL.format(field=field))


def _plan_name(me: dict[str, Any]) -> str | None:
    """B4: the Cloud's plan is an object (`{"plan", "source", "expires_at"}`) or null."""
    return me["plan"]["plan"] if me["plan"] else None
