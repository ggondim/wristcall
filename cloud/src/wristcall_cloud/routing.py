"""Node choice for routes: pure functions plus the in-memory record of failure reports.

No I/O here. The route endpoint reads the node documents and calls `choose_nodes`; `NodeReports` is the only state.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .cloud_store import ONE_WAY
from .config import CloudConfig

CONVERSATION = "conversation"
ONE_WAY_KIND = "one_way"


class NodeReports:
    """Which accounts reported each node as failing, kept for `window_s` seconds."""

    def __init__(self, window_s: float, accounts: int) -> None:
        self.window_s = window_s
        self.accounts = accounts
        self._seen: dict[str, dict[str, float]] = {}

    def record(self, node_id: str, account: str, now: float) -> None:
        by_account = self._seen.setdefault(node_id, {})
        by_account[account] = now
        # Every node, not only this one: reports for a node nobody asks about any more must not pile up.
        for seen in list(self._seen):
            self._prune(seen, now)

    def count(self, node_id: str, now: float) -> int:
        """Distinct accounts that reported the node within the window."""
        self._prune(node_id, now)
        return len(self._seen.get(node_id, ()))

    def blocks(self, node_id: str, now: float) -> bool:
        return self.count(node_id, now) >= self.accounts

    def _prune(self, node_id: str, now: float) -> None:
        by_account = self._seen.get(node_id)
        if by_account is None:
            return
        for account in [a for a, at in by_account.items() if now - at >= self.window_s]:
            del by_account[account]
        if not by_account:
            del self._seen[node_id]


@dataclass(frozen=True)
class RouteNeed:
    """What a route request asks of a node. `kind` is None for `manage` (no capacity check)."""

    plan: str | None
    kind: str | None
    country: str | None
    avoid: frozenset[str]


def call_kind(call_type: str) -> str:
    """Capacity bucket of a call type: `conversation` or `one_way` (one-shot and monologue)."""
    return ONE_WAY_KIND if call_type in ONE_WAY else CONVERSATION


def region_order(country: str | None, table: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Regions to try, best first, for a country; unknown or missing countries use the `*` row."""
    if country is not None and country in table:
        return table[country]
    return table["*"]


def is_healthy(
    node: dict, need: RouteNeed, *, config: CloudConfig, reports: NodeReports, started_at: float, now: float
) -> bool:
    if node.get("kind") == "on_demand":
        return not reports.blocks(node["_id"], now)
    last_seen = node.get("last_seen_at")
    if last_seen is None:
        return False
    fresh = now - last_seen < config.node_fresh_s
    # Right after the Cloud starts, nodes had no chance to heartbeat yet: trust the recently seen ones.
    startup = now - started_at < config.route_startup_grace_s and started_at - last_seen <= config.node_stale_s
    if not (fresh or startup):
        return False
    if need.kind is not None:
        limit = (node.get("capacity") or {}).get(need.kind)
        if limit is not None and (node.get("active_by_type") or {}).get(need.kind, 0) >= limit:
            return False
    return True


def choose_nodes(
    nodes: list[dict], need: RouteNeed, *, config: CloudConfig, reports: NodeReports, started_at: float, now: float
) -> list[dict]:
    """Healthy candidates, best first: the first serves the request, the second is the fallback."""
    order = region_order(need.country, config.route_regions)
    voices = need.plan == "full" and need.kind is not None
    picked = []
    for node in nodes:
        if not node.get("enabled") or node["_id"] in need.avoid:
            continue
        if voices and not (
            {"operator_stt", "operator_tts"} <= set(node.get("capabilities") or ())
            and node.get("providers_ok") is not False
        ):
            continue
        if is_healthy(node, need, config=config, reports=reports, started_at=started_at, now=now):
            picked.append(node)

    def key(node: dict) -> tuple:
        region = node.get("region")
        position = order.index(region) if region in order else len(order)
        load = sum((node.get("active_by_type") or {}).values())
        return position, node.get("priority", 0), load, node["_id"]

    return sorted(picked, key=key)
