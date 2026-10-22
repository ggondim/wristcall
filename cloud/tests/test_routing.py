from __future__ import annotations

from wristcall_cloud.config import CloudConfig
from wristcall_cloud.routing import (
    NodeReports,
    RouteNeed,
    call_kind,
    choose_nodes,
    is_healthy,
    region_order,
)

NOW = 1_000_000.0
STARTED = NOW - 3600
CONFIG = CloudConfig(mongo_url="mongodb://unused")


def node(node_id="n1", **over):
    doc = {
        "_id": node_id,
        "url": f"https://{node_id}.example.test",
        "region": "iad",
        "kind": "always_on",
        "capabilities": ["operator_stt", "operator_tts"],
        "priority": 100,
        "enabled": True,
        "last_seen_at": NOW - 5,
    }
    doc.update(over)
    return doc


def need(plan="custom", kind="conversation", country=None, avoid=()):
    return RouteNeed(plan=plan, kind=kind, country=country, avoid=frozenset(avoid))


def reports():
    return NodeReports(CONFIG.node_reports_window_s, CONFIG.node_reports_accounts)


def healthy(doc, n=None, *, rep=None, started_at=STARTED, now=NOW):
    return is_healthy(
        doc, n or need(), config=CONFIG, reports=rep or reports(), started_at=started_at, now=now
    )


def choose(nodes, n=None, *, rep=None, started_at=STARTED, now=NOW):
    return [
        d["_id"]
        for d in choose_nodes(
            nodes, n or need(), config=CONFIG, reports=rep or reports(), started_at=started_at, now=now
        )
    ]


def test_call_kind():
    assert call_kind("conversation") == "conversation"
    assert call_kind("one-shot") == "one_way"
    assert call_kind("monologue") == "one_way"


def test_region_order():
    table = CONFIG.route_regions
    assert region_order("BR", table) == ("sao", "gru")
    assert region_order("US", table) == ("iad",)
    assert region_order("JP", table) == table["*"]
    assert region_order(None, table) == table["*"]


def test_always_on_needs_a_fresh_heartbeat():
    assert healthy(node(last_seen_at=NOW - 44))
    assert not healthy(node(last_seen_at=NOW - 46))
    doc = node()
    del doc["last_seen_at"]
    assert not healthy(doc)


def test_startup_grace_keeps_recent_nodes_only():
    started = NOW - 30
    assert healthy(node(last_seen_at=started - 70), started_at=started)
    assert not healthy(node(last_seen_at=started - 3600), started_at=started)
    started = NOW - 61
    assert not healthy(node(last_seen_at=started - 70), started_at=started)


def test_capacity_by_kind():
    doc = node(
        active_by_type={"conversation": 3, "one_way": 0},
        capacity={"conversation": 3, "one_way": None},
    )
    assert not healthy(doc, need(kind="conversation"))
    assert healthy(doc, need(kind="one_way"))
    assert healthy(doc, need(kind=None))


def test_on_demand_needs_three_accounts_to_go_unhealthy():
    doc = node(kind="on_demand")
    del doc["last_seen_at"]
    rep = reports()
    assert healthy(doc, rep=rep)
    for _ in range(5):
        rep.record("n1", "a", NOW)
    assert healthy(doc, rep=rep)
    rep.record("n1", "b", NOW)
    rep.record("n1", "c", NOW)
    assert rep.count("n1", NOW) == 3
    assert rep.blocks("n1", NOW)
    assert not healthy(doc, rep=rep)
    assert healthy(doc, rep=rep, now=NOW + 301)
    assert rep.count("n1", NOW + 301) == 0
    assert rep.count("other", NOW) == 0


def test_on_demand_ignores_capacity():
    doc = node(kind="on_demand", active_by_type={"conversation": 9}, capacity={"conversation": 1})
    assert healthy(doc)


def test_full_call_needs_operator_voices():
    n = need(plan="full", kind="one_way")
    stt_only = node("a", capabilities=["operator_stt"])
    both = node("b")
    broken = node("c", providers_ok=False)
    assert choose([stt_only, both, broken], n) == ["b"]
    for other in (need(plan="custom", kind="one_way"), need(plan="full", kind=None)):
        assert choose([stt_only, both, broken], other) == ["a", "b", "c"]


def test_order_by_region_priority_load():
    home = node("home", region="sao", priority=10)
    gru1 = node("gru1", region="gru")
    iad1 = node("iad1", region="iad")
    nodes = [iad1, gru1, home]
    assert choose(nodes, need(country="BR")) == ["home", "gru1", "iad1"]
    assert choose(nodes, need(country="US"))[0] == "iad1"
    assert choose(nodes, need(country=None))[0] == "iad1"

    busy = node("iad0", active_by_type={"conversation": 2, "one_way": 1})
    idle = node("iad2", active_by_type={"conversation": 0, "one_way": 0})
    assert choose([busy, idle], need(country="US")) == ["iad2", "iad0"]

    a, b = node("b-node"), node("a-node")
    assert choose([a, b], need(country="US")) == ["a-node", "b-node"]


def test_avoid_and_disabled_are_left_out():
    home = node("home", region="sao", priority=10)
    other = node("other")
    off = node("off", enabled=False)
    assert choose([home, other, off], need(country="BR")) == ["home", "other"]
    assert choose([home, other, off], need(country="BR", avoid={"home"})) == ["other"]


def test_record_prunes_every_node():
    # Reports for a node nobody asks about any more must not pile up.
    rep = reports()
    rep.record("a", "acc1", NOW)
    rep.record("b", "acc2", NOW + 301)
    assert "a" not in rep._seen
    assert rep.count("b", NOW + 301) == 1
