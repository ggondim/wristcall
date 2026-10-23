"""The ntfy alert for an always-on node that stopped sending heartbeats (`node_alerts`)."""

import asyncio
import logging
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from conftest import serve
from node_fixtures import node_config, signing_key  # noqa: F401
from node_fixtures import build_node_app
from wristcall_cloud import app as app_module
from wristcall_cloud import node_alerts
from wristcall_cloud.account_store import AccountStore
from wristcall_cloud.cloud_store import CloudModeStore
from wristcall_cloud.config import CloudConfig
from wristcall_cloud.node_alerts import alert_loop, alert_round, due_alerts

URL = "https://ntfy.test/secret-topic"
BRT = ZoneInfo("America/Sao_Paulo")
DAYTIME = datetime(2026, 10, 23, 10, 0, tzinfo=BRT).timestamp()  # 10:00 in Brasilia
NIGHT = datetime(2026, 10, 23, 2, 0, tzinfo=BRT).timestamp()
STARTED = DAYTIME - 3600
MIN = 60


def node(node_id="home", *, seen, enabled=True, kind="always_on"):
    return {"_id": node_id, "enabled": enabled, "kind": kind, "last_seen_at": seen}


def alerts(nodes, now=DAYTIME, *, plan=True, down=(), after_s=900, started_at=STARTED):
    return due_alerts(nodes, now, started_at=started_at, has_active_plan=plan, after_s=after_s, down=set(down))


# ---------- due_alerts (pure) ----------


def test_due_alerts_daytime_with_plan():
    assert alerts([node(seen=DAYTIME - 16 * MIN)]) == (["home"], [])
    assert alerts([node(seen=DAYTIME - 14 * MIN)]) == ([], [])
    assert alerts([node(seen=DAYTIME - 900)]) == ([], [])  # exactly after_s: not yet


def test_due_alerts_sorts_the_ids():
    nodes = [node("b", seen=0.0), node("a", seen=0.0), node("c", seen=DAYTIME)]
    assert alerts(nodes) == (["a", "b"], [])


def test_node_never_heard_from_counts_from_the_cloud_start():
    assert alerts([node(seen=None)], started_at=DAYTIME - 16 * MIN) == (["home"], [])
    assert alerts([node(seen=None)], started_at=DAYTIME - 14 * MIN) == ([], [])


def test_no_alert_at_night():
    old = [node(seen=NIGHT - 3600)]
    assert alerts(old, NIGHT) == ([], [])
    # The window is [07:00, 23:00) in Brasilia, whatever the date's UTC hour.
    for hour, due in ((6, False), (7, True), (22, True), (23, False)):
        now = datetime(2026, 10, 23, hour, 30, tzinfo=BRT).timestamp()
        assert bool(alerts([node(seen=now - 3600)], now)[0]) is due, hour


def test_no_alert_without_active_plan():
    assert alerts([node(seen=DAYTIME - 3600)], plan=False) == ([], [])


def test_no_alert_for_disabled_or_on_demand():
    old = DAYTIME - 3600
    assert alerts([node(seen=old, enabled=False), node("od", seen=old, kind="on_demand")]) == ([], [])


def test_down_node_is_not_announced_again_and_comes_back_at_any_hour():
    assert alerts([node(seen=DAYTIME - 3600)], down={"home"}) == ([], [])
    assert alerts([node(seen=NIGHT - 10)], NIGHT, down={"home"}, plan=False) == ([], ["home"])
    assert alerts([node(seen=NIGHT - 3600)], NIGHT, down={"home"}) == ([], [])
    assert alerts([node(seen=None)], down={"home"}) == ([], [])  # never heard from: not back


# ---------- the round and the loop ----------


class Ntfy:
    """A recording ntfy: each request gets the next scripted answer (an exception is raised); 200 after the script."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    @property
    def sent(self) -> list[tuple[str, str, str, str, str]]:
        return [
            (
                r.content.decode(),
                r.headers["Title"],
                r.headers["Priority"],
                r.headers["Tags"],
                str(r.url),
            )
            for r in self.requests
        ]


@pytest.fixture
async def world(mongo_db):
    accounts = AccountStore(mongo_db)
    await accounts.ensure_indexes()
    cloud = CloudModeStore(mongo_db)
    await cloud.ensure_indexes()
    state = SimpleNamespace(
        config=CloudConfig(mongo_url="mongodb://x", alert_url=URL, alert_after_s=900),
        accounts=accounts,
        cloud=cloud,
        started_at=STARTED,
    )
    await accounts.grant("acc_1", "full", days=None)
    return state


async def put_node(state, node_id="home", **fields):
    own = {"url": f"https://{node_id}.test", "secret_hash": node_id}  # unique indexes
    doc = {"enabled": True, "kind": "always_on", "last_seen_at": None, **own, **fields}
    await state.cloud.nodes.replace_one({"_id": node_id}, doc, upsert=True)


async def test_alert_once_then_back(world):
    ntfy, down = Ntfy(), set()
    async with ntfy.client() as http:
        await put_node(world, last_seen_at=DAYTIME - 16 * MIN)
        await alert_round(world, http, down, now=DAYTIME)
        assert ntfy.sent == [
            ("wristcall node home: no heartbeat for 16 min", "wristcall-node", "high", "warning", URL)
        ]
        assert down == {"home"}
        await alert_round(world, http, down, now=DAYTIME + MIN)  # the second round does not repeat it
        assert len(ntfy.requests) == 1
        await put_node(world, last_seen_at=DAYTIME + 90)  # a new heartbeat
        await alert_round(world, http, down, now=DAYTIME + 2 * MIN)
        assert ntfy.sent[1:] == [
            ("wristcall node home: heartbeat back", "wristcall-node", "default", "white_check_mark", URL)
        ]
        assert down == set()
        await alert_round(world, http, down, now=DAYTIME + 3 * MIN)
        assert len(ntfy.requests) == 2


async def test_message_has_the_real_silence_in_minutes(world):
    world.config = replace(world.config, alert_after_s=1200)
    ntfy = Ntfy()
    async with ntfy.client() as http:
        await put_node(world, last_seen_at=DAYTIME - 21 * MIN - 30)
        await alert_round(world, http, set(), now=DAYTIME)
    assert ntfy.sent[0][0] == "wristcall node home: no heartbeat for 21 min"


async def test_message_of_a_node_never_heard_from_counts_from_the_cloud_start(world):
    ntfy = Ntfy()
    async with ntfy.client() as http:
        await put_node(world, last_seen_at=None)  # the Cloud started an hour before
        await alert_round(world, http, set(), now=DAYTIME)
    assert ntfy.sent[0][0] == "wristcall node home: no heartbeat for 60 min"


async def test_round_ignores_nodes_that_are_not_due(world):
    ntfy, down = Ntfy(), set()
    async with ntfy.client() as http:
        await put_node(world, "off", enabled=False, last_seen_at=0.0)
        await put_node(world, "lazy", kind="on_demand", last_seen_at=0.0)
        await put_node(world, "fine", last_seen_at=DAYTIME - MIN)
        await alert_round(world, http, down, now=DAYTIME)
        await world.accounts.revoke_entitlement("acc_1")
        await put_node(world, "silent", last_seen_at=0.0)
        await alert_round(world, http, down, now=DAYTIME)
        await world.accounts.grant("acc_1", "full", days=1, now=DAYTIME - 2 * 86400)  # expired
        await alert_round(world, http, down, now=DAYTIME)
    assert ntfy.requests == [] and down == set()


async def test_deleted_node_is_forgotten(world):
    ntfy, down = Ntfy(), {"gone"}
    async with ntfy.client() as http:
        await alert_round(world, http, down, now=DAYTIME)
    assert ntfy.requests == [] and down == set()


async def test_ntfy_failure_keeps_loop_alive(world, caplog):
    ntfy = Ntfy(httpx.ConnectError("no route to ntfy.test/secret-topic"), 500)
    await put_node(world, last_seen_at=DAYTIME - 16 * MIN)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 5:
            raise asyncio.CancelledError

    caplog.set_level(logging.WARNING, logger="wristcall_cloud.node_alerts")
    async with ntfy.client() as http:
        with pytest.raises(asyncio.CancelledError):
            await alert_loop(world, http, every_s=60, sleep=sleep, now=lambda: DAYTIME)
    # The connection error, the 500, then delivered; afterwards the node is down and nothing more is sent.
    assert sleeps == [60] * 5 and len(ntfy.requests) == 3
    assert "ConnectError" in caplog.text and "HTTP 500" in caplog.text
    assert "secret-topic" not in caplog.text


async def test_loop_survives_a_failed_round(world, monkeypatch):
    rounds = []

    async def broken_round(state, http, down, *, now):
        rounds.append(now)
        if len(rounds) == 1:
            raise RuntimeError("mongo went away")

    monkeypatch.setattr(node_alerts, "alert_round", broken_round)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await alert_loop(world, None, every_s=60, sleep=sleep, now=lambda: 7.0)
    assert sleeps == [60] * 3 and rounds == [7.0, 7.0]


# ---------- the app ----------


@pytest.fixture
def started(monkeypatch):
    """Replaces the loop with one that records how the app started it."""
    calls = []

    async def fake_loop(state, http, **kwargs):
        calls.append((state, http))
        await asyncio.sleep(3600)

    monkeypatch.setattr(app_module, "alert_loop", fake_loop)
    return calls


def test_no_alert_url_no_loop(node_config, mongo_db, fake_verifier, signing_key, started):  # noqa: F811
    assert node_config.alert_url is None
    for client in serve(build_node_app(node_config, mongo_db, fake_verifier, signing_key), mongo_db):
        assert client.app.state.started_at > 0  # the app is up
    assert started == []


def test_alert_url_starts_the_loop_with_its_client(node_config, mongo_db, fake_verifier, signing_key, started):  # noqa: F811
    config = replace(node_config, alert_url=URL)
    app = build_node_app(config, mongo_db, fake_verifier, signing_key)
    for client in serve(app, mongo_db):
        [(state, http)] = started
        assert state is app.state and isinstance(http, httpx.AsyncClient) and http.timeout.read == 10.0
    assert http.is_closed
