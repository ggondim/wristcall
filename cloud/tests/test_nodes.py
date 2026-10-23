"""The node registry and the `admin nodes` commands."""

import time

import pytest

from wristcall_cloud import admin
from wristcall_cloud.account_store import AccountStore
from wristcall_cloud.cloud_store import CloudModeStore
from wristcall_cloud.nodes import (
    NODE_SECRET_PREFIX,
    ROTATION_GRACE_S,
    NodeExists,
    NodeStore,
    is_node_secret,
    parse_node_id,
    parse_node_url,
    parse_region,
)
from wristcall_cloud.session_tokens import secret_hash

T = 1_800_000_000.0


@pytest.fixture
async def cloud(mongo_db):
    c = CloudModeStore(mongo_db)
    await c.ensure_indexes()
    return c


@pytest.fixture
def nodes(cloud):
    return NodeStore(cloud)


async def add_home(nodes, **overrides):
    fields = {"url": "https://node.test", "region": "gru", "kind": "always_on", "capabilities": ["operator_stt"]}
    return await nodes.add("home", **{**fields, **overrides}, now=T)


async def test_add_stores_only_the_hash(nodes):
    doc, secret = await add_home(nodes)
    assert is_node_secret(secret) and secret.startswith(NODE_SECRET_PREFIX) and len(secret) == 51
    assert doc["_id"] == "home" and doc["secret_hash"] == secret_hash(secret)
    assert doc["enabled"] is True and doc["priority"] == 100 and doc["capabilities"] == ["operator_stt"]
    assert doc["created_at"] == T and doc["rotated_at"] is None
    stored = await nodes.cloud.nodes.find_one({"_id": "home"})
    assert stored == doc and secret not in str(stored)


@pytest.mark.parametrize(
    "overrides",
    [
        {"node_id": "Home"},
        {"url": "http://n.test"},
        {"url": "https://n.test/path?x"},
        {"url": "https://localhost:8443"},
        {"region": "br"},
        {"kind": "sometimes"},
        {"capabilities": ["teleport"]},
    ],
)
async def test_add_rejects_bad_id_url_region_kind(nodes, overrides):
    fields = {
        "node_id": "home",
        "url": "https://n.test",
        "region": "gru",
        "kind": "always_on",
        "capabilities": [],
        **overrides,
    }
    with pytest.raises(ValueError):
        await nodes.add(fields.pop("node_id"), **fields)
    assert await nodes.list() == []


def test_parsers():
    assert parse_node_id("edge-1") == "edge-1"
    for bad in ("", "1a", "a_b", "a" * 33, "Home"):
        with pytest.raises(ValueError):
            parse_node_id(bad)
    assert parse_region("gru") == "gru"
    for bad in ("GRU", "gr", "grus", "gr1"):
        with pytest.raises(ValueError):
            parse_region(bad)
    assert parse_node_url("HTTPS://Node.Test:443/", allow_loopback=False) == "https://node.test"
    assert parse_node_url("http://localhost:8080", allow_loopback=True) == "http://localhost:8080"
    with pytest.raises(ValueError):
        parse_node_url("http://localhost:8080", allow_loopback=False)


def test_is_node_secret():
    assert is_node_secret("wc_node_" + "a" * 43)
    for bad in ("", "wc_node_" + "a" * 42, "wc_node_" + "a" * 44, "wc_srv_" + "a" * 43, "wc_node_" + "!" * 43):
        assert not is_node_secret(bad)


async def test_add_duplicate_id_or_url(nodes):
    await add_home(nodes)
    with pytest.raises(NodeExists):
        await add_home(nodes, url="https://other.test")
    with pytest.raises(NodeExists):
        await nodes.add("other", url="https://node.test", region="gru", kind="on_demand", capabilities=[])
    assert [n["_id"] for n in await nodes.list()] == ["home"]


async def test_list_orders_by_priority_then_id(nodes):
    for node_id, priority in (("c", 10), ("b", 50), ("a", 50)):
        await nodes.add(
            node_id, url=f"https://{node_id}.test", region="gru", kind="on_demand", capabilities=[], priority=priority
        )
    assert [n["_id"] for n in await nodes.list()] == ["c", "a", "b"]


async def test_rotate_keeps_previous_for_grace(nodes):
    _, old = await add_home(nodes)
    new = await nodes.rotate("home", now=T)
    assert is_node_secret(new) and new != old
    assert (await nodes.by_secret(new, now=T + ROTATION_GRACE_S + 1))["_id"] == "home"
    assert (await nodes.by_secret(old, now=T + 899))["_id"] == "home"
    assert await nodes.by_secret(old, now=T + 901) is None
    stored = await nodes.cloud.nodes.find_one({"_id": "home"})
    assert stored["secret_hash"] == secret_hash(new) and stored["rotated_at"] == T
    assert new not in str(stored) and old not in str(stored)


async def test_rotate_immediately_cuts_the_old_secret(nodes):
    _, old = await add_home(nodes)
    new = await nodes.rotate("home", now=T, immediate=True)
    assert await nodes.by_secret(old, now=T) is None
    assert (await nodes.by_secret(new, now=T))["_id"] == "home"
    stored = await nodes.cloud.nodes.find_one({"_id": "home"})
    assert "prev_secret_hash" not in stored and "prev_valid_until" not in stored


async def test_rotate_twice_drops_the_oldest_secret(nodes):
    _, first = await add_home(nodes)
    second = await nodes.rotate("home", now=T)
    third = await nodes.rotate("home", now=T + 10)
    assert await nodes.by_secret(first, now=T + 20) is None
    assert await nodes.by_secret(second, now=T + 20) is not None
    assert await nodes.by_secret(third, now=T + 20) is not None


async def test_rotate_unknown_node(nodes):
    assert await nodes.rotate("nope", now=T) is None


async def test_disabled_node_is_not_found_by_secret(nodes):
    _, secret = await add_home(nodes)
    assert await nodes.set_enabled("home", False) is True
    assert await nodes.by_secret(secret, now=T) is None
    assert await nodes.set_enabled("home", True) is True
    assert (await nodes.by_secret(secret, now=T))["_id"] == "home"
    assert await nodes.set_enabled("nope", True) is False


async def test_by_secret_ignores_malformed_and_unknown(nodes):
    await add_home(nodes)
    assert await nodes.by_secret("garbage", now=T) is None
    assert await nodes.by_secret(NODE_SECRET_PREFIX + "a" * 43, now=T) is None


async def test_is_node_url_includes_disabled_nodes(nodes):
    await add_home(nodes)
    assert await nodes.is_node_url("https://node.test") is True
    assert await nodes.is_node_url("https://other.test") is False
    await nodes.set_enabled("home", False)
    assert await nodes.is_node_url("https://node.test") is True


async def cli(mongo_db, capsys, *argv):
    store = AccountStore(mongo_db)
    code = await admin.run(admin.build_parser().parse_args(list(argv)), store)
    out = capsys.readouterr()
    return code, out.out, out.err


def parsed(out: str) -> dict[str, str]:
    return dict(line.split(": ", 1) for line in out.splitlines())


async def test_cli_nodes_add_list_rotate_disable(mongo_db, nodes, capsys):
    code, out, err = await cli(
        mongo_db, capsys, "nodes", "add", "home", "--url", "https://node.test", "--region", "gru",
        "--kind", "always_on", "--capability", "operator_stt", "--capability", "operator_tts", "--priority", "5",
    )  # fmt: skip
    shown = parsed(out)
    assert code == 0 and err == "" and shown["node"] == "home" and shown["url"] == "https://node.test"
    assert is_node_secret(shown["secret"]) and out.count("secret:") == 1
    stored = await nodes.cloud.nodes.find_one({"_id": "home"})
    assert stored["capabilities"] == ["operator_stt", "operator_tts"] and stored["priority"] == 5
    assert stored["secret_hash"] == secret_hash(shown["secret"])

    code, out, err = await cli(mongo_db, capsys, "nodes", "list")
    assert code == 0 and err == ""
    assert out == "node: home url=https://node.test region=gru kind=always_on enabled=true last_seen_at=never\n"
    assert shown["secret"] not in out and stored["secret_hash"] not in out

    code, out, _ = await cli(mongo_db, capsys, "nodes", "rotate", "home")
    rotated = parsed(out)
    assert code == 0 and is_node_secret(rotated["secret"]) and rotated["secret"] != shown["secret"]
    assert out.count("secret:") == 1
    assert (await nodes.by_secret(shown["secret"])) is not None
    code, out, _ = await cli(mongo_db, capsys, "nodes", "rotate", "home", "--now")
    assert code == 0 and is_node_secret(parsed(out)["secret"])
    assert (await nodes.by_secret(rotated["secret"])) is None

    code, out, _ = await cli(mongo_db, capsys, "nodes", "disable", "home")
    assert code == 0 and parsed(out) == {"enabled": "false"}
    code, out, _ = await cli(mongo_db, capsys, "nodes", "list")
    assert "enabled=false" in out
    code, out, _ = await cli(mongo_db, capsys, "nodes", "enable", "home")
    assert code == 0 and parsed(out) == {"enabled": "true"}


async def test_cli_nodes_errors(mongo_db, nodes, capsys):
    for argv in (("rotate", "nope"), ("disable", "nope"), ("enable", "nope")):
        code, out, err = await cli(mongo_db, capsys, "nodes", *argv)
        assert code == 1 and out == "" and "no such node" in err

    add = ("nodes", "add", "home", "--url", "https://node.test", "--region", "gru", "--kind", "on_demand")
    assert (await cli(mongo_db, capsys, *add))[0] == 0
    code, out, err = await cli(mongo_db, capsys, *add)
    assert code == 2 and out == "" and "node already exists" in err
    code, out, err = await cli(mongo_db, capsys, *add[:2], "other", *add[3:])
    assert code == 2 and "node already exists" in err

    code, out, err = await cli(mongo_db, capsys, "nodes", "add", "Bad", *add[3:])
    assert code == 2 and out == "" and "https://node.test" not in err
    loopback = ("nodes", "add", "dev", "--url", "http://localhost:9000", "--region", "gru", "--kind", "on_demand")
    code, out, err = await cli(mongo_db, capsys, *loopback)
    assert code == 2 and "localhost" not in err
    assert (await cli(mongo_db, capsys, *loopback, "--allow-loopback"))[0] == 0
    assert len(await nodes.list()) == 2


async def test_cli_nodes_add_creates_the_indexes_first(mongo_db, capsys):
    """Before the Cloud's first start on this version nothing created the unique URL index: the command does."""
    add = ("nodes", "add", "home", "--url", "https://node.test", "--region", "gru", "--kind", "on_demand")
    assert (await cli(mongo_db, capsys, *add))[0] == 0
    assert "url" in await mongo_db["nodes"].index_information()
    code, out, err = await cli(mongo_db, capsys, *add[:2], "other", *add[3:])
    assert code == 2 and out == "" and "node already exists" in err
    assert await mongo_db["nodes"].count_documents({}) == 1


STATS_KEYS = [
    "node", "hours", "calls", "busy", "not_ready", "busy_pct", "peak", "first_audio_p50_ms", "first_audio_p95_ms",
    "stt_p95_ms", "tts_p95_ms", "provider_failures", "cloud_unreachable_s", "outbox_dropped",
    "hours_without_heartbeat_day", "days_at_peak",
]  # fmt: skip
ROUTE_KEYS = ["routes_call", "no_node", "no_node_pct"]


async def test_admin_nodes_stats_prints_na_without_data(mongo_db, nodes, capsys):
    await nodes.add("home", url="https://node.test", region="gru", kind="always_on", capabilities=[])
    code, out, err = await cli(mongo_db, capsys, "nodes", "stats", "home")
    assert code == 0 and err == ""
    assert [line.split(": ")[0] for line in out.splitlines()] == STATS_KEYS + ROUTE_KEYS
    shown = parsed(out)
    assert shown["node"] == "home" and shown["hours"] == "24"
    assert shown["first_audio_p95_ms"] == "n/a" and shown["busy_pct"] == "n/a" and shown["no_node_pct"] == "n/a"
    assert shown["calls"] == "conversation=0 one_way=0" and shown["stt_p95_ms"] == "none"
    assert shown["provider_failures"] == "none" and shown["hours_without_heartbeat_day"] == "0"


async def test_admin_nodes_stats_sums_the_window(mongo_db, nodes, capsys):
    from wristcall_cloud.node_stats import record_heartbeat, record_route

    await add_home(nodes)  # registered long ago (T)
    await nodes.add("away", url="https://away.test", region="gru", kind="always_on", capabilities=[], now=T)
    await nodes.add("off", url="https://off.test", region="gru", kind="always_on", capabilities=[], now=T)
    await nodes.set_enabled("off", False)
    now = time.time()
    snapshot = {
        "calls": {"conversation": 3, "one_way": 0}, "busy": {"conversation": 1, "one_way": 0},
        "first_audio_ms": {"1000": 9, "inf": 1}, "stt_ms": {"stt-default": {"500": 1}},
        "provider_failures": {"user": 2}, "cloud_unreachable_s": 40,
    }  # fmt: skip
    collection = nodes.cloud.node_stats
    await record_heartbeat(collection, "home", now, {"conversation": 2, "one_way": 0}, snapshot,
                           capacity={"conversation": 2, "one_way": None})  # fmt: skip
    await record_heartbeat(collection, "home", now - 3 * 3600, {"conversation": 0, "one_way": 0}, snapshot)
    await record_heartbeat(collection, "home", now - 30 * 3600, {"conversation": 0, "one_way": 0}, snapshot)
    for no_node in (True, False, False, False):
        await record_route(collection, now, no_node=no_node)

    code, out, err = await cli(mongo_db, capsys, "nodes", "stats", "home", "--hours", "4")
    shown = parsed(out)
    assert code == 0 and err == "" and shown["hours"] == "4"
    assert shown["calls"] == "conversation=6 one_way=0" and shown["busy_pct"] == "25.0"
    assert shown["first_audio_p50_ms"] == "1000" and shown["first_audio_p95_ms"] == ">15000"
    assert shown["stt_p95_ms"] == "stt-default=500" and shown["provider_failures"] == "user=4"
    assert shown["peak"] == "conversation=2 one_way=0" and shown["days_at_peak"] == "1"
    assert shown["cloud_unreachable_s"] == "80"
    assert shown["routes_call"] == "3" and shown["no_node"] == "1" and shown["no_node_pct"] == "25.0"

    code, out, err = await cli(mongo_db, capsys, "nodes", "stats")  # every enabled node
    assert code == 0 and [line for line in out.splitlines() if line.startswith("node: ")] == [
        "node: away", "node: home",
    ]  # fmt: skip
    assert out.count("routes_call: ") == 1


async def test_admin_nodes_stats_errors(mongo_db, nodes, capsys):
    code, out, err = await cli(mongo_db, capsys, "nodes", "stats", "nope")
    assert code == 1 and out == "" and "no such node" in err
    for hours in ("0", "2161", "x"):
        with pytest.raises(SystemExit) as e:
            admin.build_parser().parse_args(["nodes", "stats", "--hours", hours])
        assert e.value.code == 2
    assert admin.build_parser().parse_args(["nodes", "stats", "--hours", "2160"]).hours == 2160
