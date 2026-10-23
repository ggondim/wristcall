"""The hourly summary of the nodes (`node_stats`): what each heartbeat's `stats` brought, and the route's counters.

Like a shift log: each node has one page per hour (UTC), and every heartbeat the Cloud accepts adds its counts to the
page of the hour it arrived in. The route keeps its own page (`node = "_route"`, an id no node can have). Pages are
dropped 90 days after their hour (TTL index on `purge_at`).

The counts come from the node (server 0.11.0, `wristcall.cloud_node.stats`); the contract is copied here, the Cloud
does not import the server. Metrics never take a node or a call down: `stats` that do not follow the contract are
ignored (the caller logs why, never the values), and a failed write is logged and changes no answer.
"""

import logging
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("wristcall_cloud.node_stats")

# The upper bounds (ms) of the histograms' buckets, then "inf" (as the node sends them).
BUCKETS_MS = (250, 500, 750, 1000, 1500, 2000, 2500, 3000, 4000, 5000, 7500, 10000, 15000)
BUCKET_KEYS = frozenset(str(bound) for bound in BUCKETS_MS) | {"inf"}
KINDS = ("conversation", "one_way")
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")  # a provider name: never "." nor "$"
MAX_NAMES = 8  # per map: up to six of the operator's, `user` and `other`
MAX_VALUE = 1_000_000_000  # any one count in one heartbeat
RETENTION = timedelta(days=90)
ROUTE = "_route"  # node ids start with a letter
DAY_TZ = ZoneInfo("America/Sao_Paulo")
DAY_HOURS = range(7, 23)  # 07:00 to 23:00, local time: when a node that is down is noticed
MAX_HOURS = 2160  # 90 days


class InvalidStats(ValueError):
    """`stats` that do not follow the contract; the message says why and never carries a value."""


# ---------- the contract ----------


def _count(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_VALUE:
        raise InvalidStats(f"{name} must be whole numbers from 0 to {MAX_VALUE}")
    return value


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidStats(f"{name} must be an object")
    return value


def _by_kind(value: Any, name: str) -> dict[str, int]:
    """A call type it does not know is left out (a newer node may count more), like an unknown top-level key."""
    counts = _object(value, name)
    return {kind: _count(n, name) for kind, n in counts.items() if kind in KINDS}


def _histogram(value: Any, name: str) -> dict[str, int]:
    counts = _object(value, name)
    if any(key not in BUCKET_KEYS for key in counts):
        raise InvalidStats(f"{name} keys must be bucket bounds")
    return {key: _count(n, name) for key, n in counts.items()}


def _names(value: Any, name: str) -> dict[str, Any]:
    by_name = _object(value, name)
    if len(by_name) > MAX_NAMES:
        raise InvalidStats(f"{name} must have at most {MAX_NAMES} providers")
    if any(not isinstance(key, str) or not NAME.fullmatch(key) for key in by_name):
        raise InvalidStats(f"{name} provider names must be lowercase letters, digits, - and _")
    return by_name


def parse_stats(value: Any) -> dict[str, Any]:
    """The heartbeat's `stats`, checked; raises InvalidStats. A key it does not know is left out (a newer node may
    send more), and one it knows but is missing counts as nothing."""
    stats = _object(value, "stats")
    out: dict[str, Any] = {}
    for key in ("calls", "busy", "peak"):
        if key in stats:
            out[key] = _by_kind(stats[key], key)
    for key in ("not_ready", "cloud_unreachable_s", "outbox_dropped"):
        if key in stats:
            out[key] = _count(stats[key], key)
    if "first_audio_ms" in stats:
        out["first_audio_ms"] = _histogram(stats["first_audio_ms"], "first_audio_ms")
    for key in ("stt_ms", "tts_ms"):
        if key in stats:
            out[key] = {name: _histogram(h, key) for name, h in _names(stats[key], key).items()}
    if "provider_failures" in stats:
        failures = _names(stats["provider_failures"], "provider_failures")
        out["provider_failures"] = {name: _count(n, "provider_failures") for name, n in failures.items()}
    return out


# ---------- writing ----------


def hour_of(ts: float) -> datetime:
    """The UTC hour a timestamp falls in."""
    return datetime.fromtimestamp(ts, UTC).replace(minute=0, second=0, microsecond=0)


def _doc_id(node: str, hour: datetime) -> str:
    return f"{node}|{hour:%Y-%m-%dT%H}"


def _increments(stats: dict[str, Any]) -> dict[str, int]:
    """The `$inc` of the counts (zeros left out)."""
    inc: dict[str, int] = {}
    for key in ("calls", "busy", "first_audio_ms", "provider_failures"):
        for sub, n in stats.get(key, {}).items():
            inc[f"{key}.{sub}"] = n
    for key in ("stt_ms", "tts_ms"):
        for name, counts in stats.get(key, {}).items():
            for bound, n in counts.items():
                inc[f"{key}.{name}.{bound}"] = n
    for key in ("not_ready", "cloud_unreachable_s", "outbox_dropped"):
        inc[key] = stats.get(key, 0)
    return {path: n for path, n in inc.items() if n}


async def _upsert(collection: Any, node: str, now: float, inc: dict[str, int], high: dict[str, int]) -> None:
    hour = hour_of(now)
    update: dict[str, Any] = {"$setOnInsert": {"node": node, "hour": hour, "purge_at": hour + RETENTION}}
    if inc:
        update["$inc"] = inc
    if high:
        update["$max"] = high
    await collection.update_one({"_id": _doc_id(node, hour)}, update, upsert=True)


async def record_heartbeat(
    collection: Any,
    node_id: str,
    now: float,
    active_by_type: dict[str, int],
    stats: dict[str, Any] | None,
    *,
    capacity: dict[str, int | None] | None = None,
) -> None:
    """Adds an accepted heartbeat to its node's hour, in one upsert: `beats` + 1, `peak` and `capacity` by type
    (`$max`; a null capacity is no limit and is left out), and the counts of `stats` (already parsed; None: none).
    Never raises: a failure is logged (its type only)."""
    sent_peak = (stats or {}).get("peak", {})
    high = {f"peak.{kind}": max(active_by_type.get(kind, 0), sent_peak.get(kind, 0)) for kind in KINDS}
    for kind, limit in (capacity or {}).items():
        if limit is not None:
            high[f"capacity.{kind}"] = limit
    try:
        await _upsert(collection, node_id, now, {"beats": 1, **_increments(stats or {})}, high)
    except Exception as e:  # metrics never fail a heartbeat
        log.warning("recording the stats of node %s failed (%s)", node_id, type(e).__name__)


async def record_route(collection: Any, now: float, *, no_node: bool) -> None:
    """Counts a call route: issued (`routes_call`) or refused for want of a node (`no_node`). Never raises."""
    try:
        await _upsert(collection, ROUTE, now, {"no_node" if no_node else "routes_call": 1}, {})
    except Exception as e:  # metrics never fail a route
        log.warning("recording a route failed (%s)", type(e).__name__)


# ---------- reading ----------


def _bound(key: str) -> float:
    return math.inf if key == "inf" else int(key)


def percentile(counts: dict[str, int], pct: int) -> int | float | None:
    """The upper bound of the bucket the `pct` percentile falls in (`math.inf` past the last bound); None without
    samples."""
    total = sum(counts.values())
    if total <= 0:
        return None
    rank = -(-pct * total // 100)  # ceil(pct% of total), in integers
    seen = 0
    key = ""
    for key in sorted(counts, key=_bound):  # total > 0, so the last bucket always reaches the rank
        seen += counts[key]
        if seen >= rank:
            break
    bound = _bound(key)
    return bound if bound == math.inf else int(bound)


def _merge(into: dict[str, int], counts: dict[str, Any] | None) -> None:
    for key, n in (counts or {}).items():
        if isinstance(n, int):
            into[key] = into.get(key, 0) + n


def _pct(part: int, whole: int) -> float | None:
    return None if whole == 0 else round(100 * part / whole, 1)


def finished_day_hours(start: datetime, now: float) -> list[datetime]:
    """The UTC hours from `start` that have ended by `now` and begin between 07:00 and 23:00 in Brasília."""
    hours = []
    hour = start
    while (hour + timedelta(hours=1)).timestamp() <= now:
        if hour.astimezone(DAY_TZ).hour in DAY_HOURS:
            hours.append(hour)
        hour += timedelta(hours=1)
    return hours


def summarize(
    docs: list[dict[str, Any]], *, start: datetime, now: float, node_kind: str = "always_on"
) -> dict[str, Any]:
    """The summary of one node's hours from `start` (a UTC hour) to `now` (pure). Percentiles are None without
    samples, percentages None when nothing was counted, and `hours_without_heartbeat_day` None for a node that is not
    `always_on` (an on-demand node does not beat)."""
    calls = dict.fromkeys(KINDS, 0)
    busy = dict.fromkeys(KINDS, 0)
    peak = dict.fromkeys(KINDS, 0)
    first_audio: dict[str, int] = {}
    times: dict[str, dict[str, dict[str, int]]] = {"stt_ms": {}, "tts_ms": {}}
    failures: dict[str, int] = {}
    totals = {"not_ready": 0, "cloud_unreachable_s": 0, "outbox_dropped": 0}
    beaten: set[datetime] = set()
    peak_days: set[Any] = set()
    for doc in docs:
        hour = doc["hour"] if doc["hour"].tzinfo else doc["hour"].replace(tzinfo=UTC)
        if doc.get("beats", 0) > 0:
            beaten.add(hour)
        _merge(calls, doc.get("calls"))
        _merge(busy, doc.get("busy"))
        for kind, n in (doc.get("peak") or {}).items():
            if kind in peak and isinstance(n, int):
                peak[kind] = max(peak[kind], n)
        _merge(first_audio, doc.get("first_audio_ms"))
        for key in times:
            for name, counts in (doc.get(key) or {}).items():
                _merge(times[key].setdefault(name, {}), counts)
        _merge(failures, doc.get("provider_failures"))
        for key in totals:
            value = doc.get(key, 0)
            totals[key] += value if isinstance(value, int) else 0
        limit = (doc.get("capacity") or {}).get("conversation")
        if isinstance(limit, int) and limit > 0 and (doc.get("peak") or {}).get("conversation", 0) >= limit:
            peak_days.add(hour.astimezone(DAY_TZ).date())
    all_calls, all_busy = sum(calls.values()), sum(busy.values())
    return {
        "calls": calls,
        "busy": busy,
        "not_ready": totals["not_ready"],
        "busy_pct": _pct(all_busy, all_calls + all_busy),
        "peak": peak,
        "first_audio_p50_ms": percentile(first_audio, 50),
        "first_audio_p95_ms": percentile(first_audio, 95),
        "stt_p95_ms": {name: percentile(c, 95) for name, c in sorted(times["stt_ms"].items())},
        "tts_p95_ms": {name: percentile(c, 95) for name, c in sorted(times["tts_ms"].items())},
        "provider_failures": dict(sorted(failures.items())),
        "cloud_unreachable_s": totals["cloud_unreachable_s"],
        "outbox_dropped": totals["outbox_dropped"],
        "hours_without_heartbeat_day": (
            sum(1 for h in finished_day_hours(start, now) if h not in beaten) if node_kind == "always_on" else None
        ),
        "days_at_peak": len(peak_days),
    }


def summarize_routes(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """The route's counters over its hours: calls routed, refused for want of a node, and that share in %."""
    routed = sum(doc.get("routes_call", 0) for doc in docs)
    no_node = sum(doc.get("no_node", 0) for doc in docs)
    return {"routes_call": routed, "no_node": no_node, "no_node_pct": _pct(no_node, routed + no_node)}


async def hours_of(collection: Any, node: str, start: datetime) -> list[dict[str, Any]]:
    """The hour documents of `node` from `start` on, oldest first."""
    return await collection.find({"node": node, "hour": {"$gte": start}}).sort("hour", 1).to_list(None)
