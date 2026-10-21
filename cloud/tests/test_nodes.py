"""The node registry and the `admin nodes` commands."""

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
