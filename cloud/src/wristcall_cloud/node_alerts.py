"""The alert for an always-on node that stopped sending heartbeats: one ntfy message when it goes silent, one when it
is back.

Like a night watchman who only rings the bell in working hours: a node that falls silent between 23:00 and 07:00
(Brasilia) is noticed in the morning, and only while some account has a plan (nobody is waiting on the node
otherwise). A node counts as down from the moment its message was delivered (the topic URL answered 2xx) until the
message that it is back was delivered; a message ntfy refused is sent again by the next round. The set of nodes known
to be down lives in memory (one Cloud replica): after a restart a node still silent is announced once more.

The topic URL is a secret (the topic name is the password): it is never logged, and a failed POST logs only the
exception type or the status.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx
from starlette.datastructures import State

from .node_stats import DAY_HOURS, DAY_TZ

log = logging.getLogger("wristcall_cloud.node_alerts")

EVERY_S = 60
TIMEOUT_S = 10.0
TITLE = "wristcall-node"


def due_alerts(
    nodes: list[dict[str, Any]],
    now: float,
    *,
    started_at: float,
    has_active_plan: bool,
    after_s: int,
    down: set[str],
) -> tuple[list[str], list[str]]:
    """`(fell, back)`, node ids sorted. A node falls when it is enabled, always-on and silent for more than `after_s`
    (a node never heard from counts from `started_at`, the Cloud's own start), in daytime (07:00 to 23:00 in
    Brasilia), with some plan active, and is not in `down` yet. A node in `down` is back once it has beaten within
    `after_s`, at any hour. `now`, `started_at` and the nodes' `last_seen_at` are epoch seconds."""
    daytime = datetime.fromtimestamp(now, DAY_TZ).hour in DAY_HOURS
    fell: list[str] = []
    back: list[str] = []
    for node in sorted(nodes, key=lambda n: n["_id"]):
        node_id = node["_id"]
        seen = node.get("last_seen_at")
        silent_s = now - (started_at if seen is None else seen)
        if node_id in down:
            if seen is not None and silent_s <= after_s:
                back.append(node_id)
        elif (
            daytime
            and has_active_plan
            and node.get("enabled") is True
            and node.get("kind") == "always_on"
            and silent_s > after_s
        ):
            fell.append(node_id)
    return fell, back


def _fell_message(node_id: str, silent_s: float, after_s: int) -> tuple[str, dict[str, str]]:
    """The minutes are the real silence in whole minutes, never fewer than the threshold's."""
    headers = {"Title": TITLE, "Priority": "high", "Tags": "warning"}
    minutes = max(int(silent_s // 60), after_s // 60)
    return f"wristcall node {node_id}: no heartbeat for {minutes} min", headers


def _back_message(node_id: str) -> tuple[str, dict[str, str]]:
    headers = {"Title": TITLE, "Priority": "default", "Tags": "white_check_mark"}
    return f"wristcall node {node_id}: heartbeat back", headers


async def _send(http: httpx.AsyncClient, url: str, message: tuple[str, dict[str, str]]) -> bool:
    """Whether ntfy took the message (2xx); a failure is logged, never raised."""
    body, headers = message
    try:
        response = await http.post(url, content=body.encode(), headers=headers)
    except httpx.HTTPError as e:  # not the message: some carry the URL
        log.warning("alert not sent (%s)", type(e).__name__)
        return False
    if not response.is_success:
        log.warning("alert not sent (HTTP %d)", response.status_code)
        return False
    return True


async def alert_round(state: State, http: httpx.AsyncClient, down: set[str], *, now: float) -> None:
    """One look at the nodes: sends what is due and updates `down` for the messages delivered."""
    config = state.config
    projection = {"enabled": 1, "kind": 1, "last_seen_at": 1}
    nodes = await state.cloud.nodes.find({}, projection=projection).to_list(None)
    down &= {node["_id"] for node in nodes}  # a deleted node is not waited for
    fell, back = due_alerts(
        nodes,
        now,
        started_at=state.started_at,
        has_active_plan=await state.accounts.any_entitled(now=now),
        after_s=config.alert_after_s,
        down=down,
    )
    last_seen = {node["_id"]: node.get("last_seen_at") for node in nodes}
    for node_id in fell:
        seen = last_seen[node_id]
        silent_s = now - (state.started_at if seen is None else seen)
        if await _send(http, config.alert_url, _fell_message(node_id, silent_s, config.alert_after_s)):
            down.add(node_id)
    for node_id in back:
        if await _send(http, config.alert_url, _back_message(node_id)):
            down.discard(node_id)


async def alert_loop(
    state: State,
    http: httpx.AsyncClient,
    *,
    every_s: float = EVERY_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = time.time,
) -> None:
    """Every `every_s`, runs `alert_round`. Runs until cancelled; a failed round is logged and the next one tries
    again."""
    down: set[str] = set()
    while True:
        await sleep(every_s)
        try:
            await alert_round(state, http, down, now=now())
        except Exception as e:  # whatever went wrong, the next round tries again
            log.warning("checking the nodes for the alert failed (%s)", type(e).__name__)
