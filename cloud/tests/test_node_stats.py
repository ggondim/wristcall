"""The nodes' hourly summary (`node_stats`): the `stats` contract, the upserts, the summary and its TTL index."""

import math
from datetime import UTC, datetime, timedelta

import pytest
from pymongo.errors import PyMongoError

from wristcall_cloud.cloud_store import CloudModeStore
from wristcall_cloud.node_stats import (
    BUCKETS_MS,
    InvalidStats,
    finished_day_hours,
    hour_of,
    parse_stats,
    percentile,
    record_heartbeat,
    record_route,
    summarize,
    summarize_routes,
)

H10 = datetime(2026, 10, 20, 10, tzinfo=UTC)  # 07:00 in Brasília
IDLE = {"conversation": 0, "one_way": 0}


def stats(**over) -> dict:
    """What a 0.11.0 node sends (`NodeStats.snapshot`)."""
    return {
        "calls": {"conversation": 1, "one_way": 2},
        "busy": {"conversation": 0, "one_way": 1},
        "not_ready": 1,
        "peak": {"conversation": 1, "one_way": 1},
        "first_audio_ms": {"500": 2, "inf": 1},
        "stt_ms": {"stt-default": {"1000": 3}, "user": {"250": 1}},
        "tts_ms": {"tts-default": {"750": 2}},
        "provider_failures": {"stt-default": 1},
        "cloud_unreachable_s": 31,
        "outbox_dropped": 0,
        **over,
    }


@pytest.fixture
async def cloud(mongo_db):
    c = CloudModeStore(mongo_db)
    await c.ensure_indexes()
    return c


def test_buckets_match_the_node():
    assert BUCKETS_MS == (250, 500, 750, 1000, 1500, 2000, 2500, 3000, 4000, 5000, 7500, 10000, 15000)


def test_parse_stats_accepts_the_node_snapshot_and_partial_or_newer_ones():
    assert parse_stats(stats()) == stats()
    assert parse_stats({}) == {}
    assert parse_stats({"calls": {"one_way": 1}, "later": [1]}) == {"calls": {"one_way": 1}}
    # A call type it does not know is left out, like an unknown key; the known ones still count.
    assert parse_stats({"calls": {"one_way": 1, "video": 9}, "peak": {"video": 2}}) == {
        "calls": {"one_way": 1}, "peak": {},
    }
    with pytest.raises(InvalidStats):
        parse_stats({"calls": {"one_way": -1, "video": 9}})


@pytest.mark.parametrize(
    "bad",
    [
        [], "x", {"calls": [1]}, {"calls": {"conversation": -1}},
        {"busy": {"one_way": True}}, {"peak": {"one_way": 1.5}}, {"not_ready": -1}, {"outbox_dropped": "1"},
        {"cloud_unreachable_s": 10**10}, {"first_audio_ms": {"123": 1}}, {"first_audio_ms": {"500": -2}},
        {"stt_ms": {"a.b": {"500": 1}}}, {"tts_ms": {"$x": {"500": 1}}}, {"stt_ms": {"ok": {"501": 1}}},
        {"stt_ms": {"ok": 3}}, {"provider_failures": {f"p{n}": 1 for n in range(9)}},
        {"provider_failures": {"Big": 1}}, {"provider_failures": {"ok": None}},
    ],
)
def test_parse_stats_refuses_what_breaks_the_contract(bad):
    with pytest.raises(InvalidStats) as e:
        parse_stats(bad)
    assert "123" not in str(e.value) and "a.b" not in str(e.value) and "$x" not in str(e.value)


def test_summarize_percentiles_and_empty():
    empty = summarize([], start=H10, now=H10.timestamp())
    assert empty["first_audio_p95_ms"] is None and empty["first_audio_p50_ms"] is None
    assert empty["busy_pct"] is None and empty["calls"] == IDLE and empty["peak"] == IDLE
    assert empty["stt_p95_ms"] == {} and empty["provider_failures"] == {} and empty["days_at_peak"] == 0

    doc = {"hour": H10, "beats": 1, "first_audio_ms": {"1000": 9, "3000": 1}}
    summary = summarize([doc], start=H10, now=H10.timestamp())
    assert summary["first_audio_p50_ms"] == 1000 and summary["first_audio_p95_ms"] == 3000
    assert percentile({"inf": 1, "250": 1}, 95) == math.inf
    assert percentile({"inf": 1, "250": 1}, 50) == 250
    assert percentile({}, 50) is None


def test_summarize_adds_up_the_hours():
    docs = [
        {"hour": H10, "beats": 3, "calls": {"conversation": 3, "one_way": 0}, "busy": {"conversation": 1},
         "peak": {"conversation": 2, "one_way": 0}, "stt_ms": {"a": {"500": 1}}, "provider_failures": {"a": 1},
         "cloud_unreachable_s": 5, "not_ready": 2},
        {"hour": H10 + timedelta(hours=1), "beats": 1, "calls": {"one_way": 4}, "peak": {"one_way": 3},
         "stt_ms": {"a": {"2000": 1}, "b": {"inf": 1}}, "provider_failures": {"a": 2, "b": 1}, "outbox_dropped": 1},
    ]  # fmt: skip
    summary = summarize(docs, start=H10, now=(H10 + timedelta(hours=2)).timestamp())
    assert summary["calls"] == {"conversation": 3, "one_way": 4}
    assert summary["busy"] == {"conversation": 1, "one_way": 0}
    assert summary["busy_pct"] == 12.5  # 1 of 8
    assert summary["peak"] == {"conversation": 2, "one_way": 3} and summary["not_ready"] == 2
    assert summary["stt_p95_ms"] == {"a": 2000, "b": math.inf} and summary["tts_p95_ms"] == {}
    assert summary["provider_failures"] == {"a": 3, "b": 1}
    assert summary["cloud_unreachable_s"] == 5 and summary["outbox_dropped"] == 1
    assert summary["hours_without_heartbeat_day"] == 0


def test_hours_without_heartbeat_is_none_for_a_node_that_does_not_beat():
    now = (H10 + timedelta(hours=3)).timestamp()
    assert summarize([], start=H10, now=now, node_kind="on_demand")["hours_without_heartbeat_day"] is None
    assert summarize([], start=H10, now=now, node_kind="always_on")["hours_without_heartbeat_day"] == 3


def test_hours_without_heartbeat_counts_only_day_hours_brt():
    h03 = datetime(2026, 10, 20, 3, tzinfo=UTC)  # 00:00 in Brasília
    # 03:00 to 12:59 UTC have ended, none with a heartbeat: only 10, 11 and 12 UTC (07, 08, 09 BRT) count.
    now = datetime(2026, 10, 20, 13, 30, tzinfo=UTC).timestamp()
    assert summarize([], start=h03, now=now)["hours_without_heartbeat_day"] == 3
    # The hour still running does not count; one with a heartbeat does not; one with `beats: 0` does.
    docs = [{"hour": H10, "beats": 2}, {"hour": H10 + timedelta(hours=1), "beats": 0}]
    assert summarize(docs, start=h03, now=now)["hours_without_heartbeat_day"] == 2
    # 22:00 BRT (01 UTC) counts, 23:00 BRT (02 UTC) does not.
    late = finished_day_hours(datetime(2026, 10, 21, 0, tzinfo=UTC), datetime(2026, 10, 21, 3, tzinfo=UTC).timestamp())
    assert [h.hour for h in late] == [0, 1]


def test_days_at_peak_uses_the_capacity_of_the_hour():
    docs = [
        {"hour": H10, "beats": 1, "peak": {"conversation": 2}, "capacity": {"conversation": 2}},
        # Draining later: the node then offered 0, but the hour keeps the most it offered.
        {"hour": H10 + timedelta(hours=1), "beats": 1, "peak": {"conversation": 2}, "capacity": {"conversation": 2}},
        {"hour": H10 + timedelta(days=1), "beats": 1, "peak": {"conversation": 1}, "capacity": {"conversation": 2}},
        {"hour": H10 + timedelta(days=2), "beats": 1, "peak": {"conversation": 0}, "capacity": {"conversation": 0}},
        {"hour": H10 + timedelta(days=3), "beats": 1, "peak": {"conversation": 5}},  # no limit
    ]
    assert summarize(docs, start=H10, now=H10.timestamp())["days_at_peak"] == 1


def test_summarize_routes():
    assert summarize_routes([]) == {"routes_call": 0, "no_node": 0, "no_node_pct": None}
    docs = [{"routes_call": 3, "no_node": 1}, {"routes_call": 0}, {"no_node": 0}]
    assert summarize_routes(docs) == {"routes_call": 3, "no_node": 1, "no_node_pct": 25.0}


async def test_record_heartbeat_one_document_per_hour(cloud):
    now = H10.timestamp() + 600
    await record_heartbeat(cloud.node_stats, "home", now, {"conversation": 0, "one_way": 2}, stats(),
                           capacity={"conversation": None, "one_way": 2})  # fmt: skip
    await record_heartbeat(cloud.node_stats, "home", now + 60, {"conversation": 3, "one_way": 0}, None,
                           capacity={"conversation": 4, "one_way": 0})  # fmt: skip
    doc = await cloud.node_stats.find_one({"_id": "home|2026-10-20T10"})
    assert doc["node"] == "home" and doc["hour"] == H10 and doc["purge_at"] == H10 + timedelta(days=90)
    assert doc["beats"] == 2 and doc["peak"] == {"conversation": 3, "one_way": 2}
    assert doc["capacity"] == {"conversation": 4, "one_way": 2}
    assert doc["calls"] == {"conversation": 1, "one_way": 2} and doc["busy"] == {"one_way": 1}
    assert doc["first_audio_ms"] == {"500": 2, "inf": 1} and doc["not_ready"] == 1
    assert doc["stt_ms"] == {"stt-default": {"1000": 3}, "user": {"250": 1}}
    assert doc["tts_ms"] == {"tts-default": {"750": 2}} and doc["provider_failures"] == {"stt-default": 1}
    assert doc["cloud_unreachable_s"] == 31 and "outbox_dropped" not in doc
    await record_heartbeat(cloud.node_stats, "home", now + 3600, IDLE, None)
    assert await cloud.node_stats.count_documents({"node": "home"}) == 2


async def test_record_failures_are_logged_not_raised(caplog):
    class Broken:
        async def update_one(self, *args, **kwargs):
            raise PyMongoError("db.example:27017 is down")

    await record_heartbeat(Broken(), "home", H10.timestamp(), IDLE, stats())
    await record_route(Broken(), H10.timestamp(), no_node=True)
    assert caplog.text.count("PyMongoError") == 2 and "db.example" not in caplog.text


async def test_record_route_counts_on_the_route_document(cloud):
    for no_node in (True, False, False):
        await record_route(cloud.node_stats, H10.timestamp(), no_node=no_node)
    doc = await cloud.node_stats.find_one({"_id": "_route|2026-10-20T10"})
    assert doc["node"] == "_route" and doc["routes_call"] == 2 and doc["no_node"] == 1 and "beats" not in doc


async def test_node_stats_ttl_index(cloud):
    indexes = {i["name"]: i async for i in await cloud.node_stats.list_indexes()}
    assert indexes["purge_at"]["key"] == {"purge_at": 1} and indexes["purge_at"]["expireAfterSeconds"] == 0
    assert indexes["node_hour"]["key"] == {"node": 1, "hour": 1}


def test_hour_of():
    assert hour_of(datetime(2026, 10, 20, 10, 59, 59, tzinfo=UTC).timestamp()) == H10
