"""Push for the cloud-mode nodes: push targets, `POST /v1/node/push` and the 80% quota warning."""

import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from node_fixtures import (
    NODE_A,
    PUBLIC_URL,
    db,
    node_config,
    node_headers,
    register_nodes,
    served_node,
    signing_key,
    user_token,
)
from test_node_calls import World
from wristcall_cloud.node_auth import NodeCaller, issue_grant
from wristcall_cloud.node_core import close_call
from wristcall_cloud.push.limits import SendLimiter
from wristcall_cloud.push.registry import key_id, new_push_key

TOPIC = "io.github.ggondim.wristcall"
CALL = "c_0123456789abcdef"


@pytest.fixture
def push_client(node_config, mongo_db, fake_verifier, signing_key):
    # `apns_topics` so that registrations are accepted; the app takes the shared fake channel as in the other tests.
    yield from served_node(replace(node_config, apns_topics=(TOPIC,)), mongo_db, fake_verifier, signing_key)


@pytest.fixture
def push_nodes(push_client):
    return register_nodes(push_client)


class PushWorld(World):
    """A `World` whose devices (iPhone and watch) have registered for push and are pushed to by their node."""

    def register(self, n: int, events=("call.finished", "quota.warning")) -> str:
        body = {
            "platform": "apns", "token": f"{n:02x}" * 32, "topic": TOPIC, "environment": "sandbox",
            "label": "Home", "tag": "node-a", "events": list(events),
        }
        r = self.client.post("/v1/push/registrations", json=body)
        assert r.status_code == 201, r.text
        return r.json()["push_key"]

    def target(self, headers: dict, push_key: str):
        return self.client.put("/v1/node/push-targets", headers=headers, json={"push_key": push_key})

    def targets(self) -> dict[str, dict]:
        with db(self.mongo_db) as d:
            return {t["_id"]: t for t in d["cloud_push_targets"].find()}

    @property
    def fake(self):
        return self.app.state.channels["apns"]

    def grant_headers(self, device: str, platform: str, *, now: float | None = None, call_id: str = CALL) -> dict:
        caller = NodeCaller(account=self.account, device=device, platform=platform, node={"_id": "home", "url": NODE_A})
        token = issue_grant(
            self.signing_key, issuer=PUBLIC_URL, caller=caller, call_id=call_id, max_s=60,
            now=time.time() if now is None else now,
        )
        return node_headers(self.nodes["home"], grant=token)

    def push(self, headers: dict, call: str | None = CALL, **message):
        body = {"event": "call.finished", "title": "Call finished", "body": "Done", **message}
        if call is not None:
            body["call"] = call
        return self.client.post("/v1/node/push", headers=headers, json=body)

    def two_targets(self) -> tuple[str, str]:
        """Targets for the watch (token 0x02) and the iPhone (0x01); their push keys."""
        phone, watch = self.register(1), self.register(2)
        assert self.target(self.user(self.ios_device, "ios"), phone).status_code == 204
        assert self.target(self.watch, watch).status_code == 204
        return phone, watch

    def sent_tokens(self) -> list[str]:
        return sorted(reg["channel"][:2] for reg, _ in self.fake.sent)


@pytest.fixture
def pw(push_client, mongo_db, push_nodes, signing_key) -> PushWorld:
    return PushWorld(push_client, mongo_db, push_nodes, signing_key, push_client.app)


def test_put_and_delete_push_target(pw):
    phone, watch = pw.register(1), pw.register(2)
    head = pw.watch
    for bad in ("nope", "wc_push_" + "A" * 42, 5, None):
        assert pw.target(head, bad).status_code == 422, bad
    unknown = pw.target(head, new_push_key())
    assert unknown.status_code == 422 and unknown.json()["error"] == "invalid"
    assert pw.client.put("/v1/node/push-targets", headers=head, json={"push_key": watch, "x": 1}).status_code == 422
    assert pw.client.put("/v1/node/push-targets", headers=head, json=[watch]).status_code == 422
    assert pw.client.put("/v1/node/push-targets", headers={"Authorization": head["Authorization"]},
                         json={"push_key": watch}).status_code == 401
    assert pw.targets() == {}
    assert pw.target(head, phone).status_code == 204
    assert pw.target(head, watch).status_code == 204  # the second one replaces the first
    targets = pw.targets()
    assert list(targets) == [f"{pw.account}:{pw.watch_device}"]
    target = targets[f"{pw.account}:{pw.watch_device}"]
    assert target["registration"] == key_id(watch) and target["account"] == pw.account
    assert target["device"] == pw.watch_device and target["platform"] == "watchos"
    assert isinstance(target["updated_at"], float)
    # Nothing in the database holds a push key in clear.
    with db(pw.mongo_db) as d:
        for name in d.list_collection_names():
            dump = repr(list(d[name].find()))
            assert phone not in dump and watch not in dump, name
    for _ in range(2):  # idempotent
        assert pw.client.delete("/v1/node/push-targets", headers=head).status_code == 204
    assert pw.targets() == {}
    assert pw.client.delete("/v1/node/push-targets", headers={"Authorization": head["Authorization"]}).status_code == 401


def test_push_with_grant_reaches_only_the_proof_device(pw):
    pw.two_targets()
    stranger = PushWorld(pw.client, pw.mongo_db, pw.nodes, pw.signing_key, pw.app, first_agent=10)
    stranger_key = stranger.register(3)
    assert stranger.target(stranger.watch, stranger_key).status_code == 204
    r = pw.push(pw.grant_headers(pw.watch_device, "watchos"))
    assert r.status_code == 202 and r.json() == {"sent": 1}
    assert pw.sent_tokens() == ["02"]
    registration, message = pw.fake.sent[0]
    assert (message.event, message.title, message.body) == ("call.finished", "Call finished", "Done")
    assert message.subtitle == "Home" and message.tag == "node-a"
    # The iPhone's grant reaches the iPhone only; the user token does the same without a call.
    assert pw.push(pw.grant_headers(pw.ios_device, "ios")).json() == {"sent": 1}
    assert pw.push(pw.user(pw.ios_device, "ios"), call=None).json() == {"sent": 1}
    assert pw.sent_tokens() == ["01", "01", "02"]
    # The other account's target never received anything.
    assert all(registration["channel"][:2] != "03" for registration, _ in pw.fake.sent)


def test_push_with_expired_grant_is_401(pw):
    pw.two_targets()
    headers = pw.grant_headers(pw.watch_device, "watchos", now=time.time() - 7200)  # 60 s + 3600 s slack ago
    r = pw.push(headers)
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert pw.fake.sent == []


def test_push_refusals_and_quiet_outcomes(pw):
    pw.two_targets()
    grant = pw.grant_headers(pw.watch_device, "watchos")
    # A grant needs the call it names in the body (and that call must be the grant's), a node secret alone is 401.
    assert pw.push(grant, call=None).status_code == 422
    assert pw.push(grant, call="c_ffffffffffffffff").status_code == 401
    assert pw.push({"Authorization": grant["Authorization"]}, call=None).status_code == 401
    # The call is taken out of the message; any other extra field is still refused.
    assert pw.push(grant, extra=1).status_code == 422
    assert pw.push(grant, title="").status_code == 422
    assert pw.push(grant, ttl_s="soon").status_code == 422
    assert pw.fake.sent == []
    # An event the registration did not ask for is not sent, and is not an error.
    assert pw.push(grant, event="device.approval").json() == {"sent": 0}
    assert pw.push(grant, event="other").json() == {"sent": 0}
    # A device without a target.
    pw.client.delete("/v1/node/push-targets", headers=pw.watch)
    assert pw.push(grant).json() == {"sent": 0}
    assert pw.fake.sent == []


def test_push_drops_a_target_whose_device_or_registration_is_gone(pw):
    phone, watch = pw.two_targets()
    grant = pw.grant_headers(pw.watch_device, "watchos")
    assert pw.client.delete(
        "/v1/push/registrations/current", headers={"Authorization": f"Bearer {watch}"}
    ).status_code == 204
    assert pw.push(grant).json() == {"sent": 0}
    assert list(pw.targets()) == [f"{pw.account}:{pw.ios_device}"]
    # The device itself is gone from the account.
    with db(pw.mongo_db) as d:
        d["devices"].delete_one({"_id": pw.ios_device})
    assert pw.push(pw.grant_headers(pw.ios_device, "ios")).json() == {"sent": 0}
    assert pw.targets() == {}
    # The push service says the device is gone: the registration and the target go.
    gone = pw.register(7)
    assert pw.target(pw.watch, gone).status_code == 204
    pw.fake.gone.add("07" * 32)
    assert pw.push(grant).json() == {"sent": 0}
    assert pw.targets() == {}
    assert pw.client.get("/v1/push/registrations/current", headers={"Authorization": f"Bearer {gone}"}).status_code == 410


def test_push_when_the_channel_is_down_is_not_sent(pw):
    pw.two_targets()
    pw.fake.down = True
    assert pw.push(pw.grant_headers(pw.watch_device, "watchos")).json() == {"sent": 0}
    assert len(pw.targets()) == 2


def test_a_malformed_message_is_422_even_when_nothing_would_be_sent(pw):
    grant = pw.grant_headers(pw.watch_device, "watchos")
    assert pw.push(grant, title="").status_code == 422  # no target at all
    pw.two_targets()
    assert pw.push(grant, event="device.approval", title="").status_code == 422  # an event the target did not ask for
    assert pw.push(grant, event="device.approval", extra=1).status_code == 422
    assert pw.push(grant, event="device.approval").json() == {"sent": 0}
    assert pw.fake.sent == []


def test_push_send_route_unchanged(pw):
    key = pw.register(5)
    auth = {"Authorization": f"Bearer {key}"}
    body = {"event": "call.finished", "title": "Done"}
    r = pw.client.post("/v1/push/send", headers=auth, json=body)
    assert r.status_code == 202 and r.json() == {"status": "sent"}
    assert [msg.event for _, msg in pw.fake.sent] == ["call.finished"]
    # An event the registration did not ask for is invalid here, not skipped.
    assert pw.client.post("/v1/push/send", headers=auth, json={**body, "event": "device.approval"}).status_code == 422
    # The budget: one a minute here; the answer says when to come back.
    pw.app.state.send_limiter = SendLimiter(1, 100)
    assert pw.client.post("/v1/push/send", headers=auth, json=body).status_code == 202
    limited = pw.client.post("/v1/push/send", headers=auth, json=body)
    assert limited.status_code == 429 and limited.json()["error"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1
    # The channel is down: 503; an unknown key: 410; a device the channel dropped: 410 and the registration goes.
    pw.app.state.send_limiter = SendLimiter(100, 100)
    pw.fake.down = True
    assert pw.client.post("/v1/push/send", headers=auth, json=body).status_code == 503
    pw.fake.down = False
    unknown = pw.client.post("/v1/push/send", headers={"Authorization": f"Bearer {new_push_key()}"}, json=body)
    assert unknown.status_code == 410 and unknown.json()["error"] == "gone"
    pw.fake.gone.add("05" * 32)
    assert pw.client.post("/v1/push/send", headers=auth, json=body).status_code == 410
    assert pw.client.get("/v1/push/registrations/current", headers=auth).status_code == 410


def finish_with(pw: PushWorld, seconds: int, **kwargs):
    opened = pw.opened()
    r = pw.finish(opened, status="delivered", seconds=seconds, **kwargs)
    assert r.status_code == 204, r.text
    return opened


def warnings(pw: PushWorld) -> list:
    return [(reg["channel"][:2], msg) for reg, msg in pw.fake.sent if msg.event == "quota.warning"]


def test_quota_warning_once_per_month(pw):
    pw.two_targets()
    stranger = PushWorld(pw.client, pw.mongo_db, pw.nodes, pw.signing_key, pw.app, first_agent=10)
    assert stranger.target(stranger.watch, stranger.register(3)).status_code == 204
    pw.set_usage(28000)  # 80% of the 36000 s is 28800
    finish_with(pw, 700)
    assert warnings(pw) == [] and "warned_80" not in pw.usage()
    finish_with(pw, 100)  # 28800: exactly 80%
    sent = warnings(pw)
    assert sorted(token for token, _ in sent) == ["01", "02"]  # every target of the account, nobody else
    message = sent[0][1]
    month = datetime.now(UTC).strftime("%Y-%m")
    assert (message.title, message.body) == ("wristcall Cloud", "You have used 80% of this month's minutes.")
    assert message.data == {"month": month, "used_s": 28800, "quota_s": 36000}
    assert isinstance(pw.usage()["warned_80"], float)
    finish_with(pw, 500)
    assert len(warnings(pw)) == 2
    # Another month starts again: its own document, its own warning.
    started = datetime(2025, 12, 31, 23, 59, 0, tzinfo=UTC).timestamp()
    with db(pw.mongo_db) as d:
        d["usage"].insert_one({"_id": f"{pw.account}:2025-12", "account": pw.account, "month": "2025-12",
                               "total_s": 29000})
    opened = pw.opened()
    with db(pw.mongo_db) as d:
        d["cloud_calls"].update_one({"_id": opened["call_id"]}, {"$set": {"created_at": started}})
    assert pw.finish(opened, status="delivered", seconds=10).status_code == 204
    again = warnings(pw)
    assert len(again) == 4 and again[-1][1].data == {"month": "2025-12", "used_s": 29010, "quota_s": 36000}


def test_quota_warning_failures_never_fail_the_close(pw):
    pw.two_targets()
    pw.fake.down = True
    pw.set_usage(28800)
    finish_with(pw, 5)  # the channel is down: the warning is lost, the call closes
    assert pw.usage()["total_s"] == 28805 and "warned_80" in pw.usage()
    pw.fake.down = False
    pw.set_usage(100)
    config = pw.app.state.config

    async def raising(account, usage):
        raise RuntimeError("boom")

    opened = pw.opened()

    async def run():
        cloud = pw.app.state.cloud
        call = await cloud.calls.find_one({"_id": opened["call_id"]})
        await cloud.usage.update_one({"account": pw.account}, {"$set": {"total_s": 40000}, "$unset": {"warned_80": ""}})
        return await close_call(
            cloud, config, call, status="delivered", error=None, seconds=1, closed_by="node", now=time.time(),
            on_quota_warning=raising,
        )

    assert pw.client.portal.call(run) is True  # the callback failed: logged, not raised
    assert "warned_80" in pw.usage()


def test_quota_warning_is_called_once_with_the_usage(pw):
    pw.set_usage(28790)
    config = pw.app.state.config
    calls: list = []

    async def record(account, usage):
        calls.append((account, usage))

    opened = pw.opened()

    async def run():
        cloud = pw.app.state.cloud
        call = await cloud.calls.find_one({"_id": opened["call_id"]})
        return await close_call(
            cloud, config, call, status="delivered", error=None, seconds=10, closed_by="node", now=time.time(),
            on_quota_warning=record,
        )

    assert pw.client.portal.call(run) is True
    assert pw.client.portal.call(run) is False
    month = datetime.now(UTC).strftime("%Y-%m")
    assert calls == [(pw.account, {"month": month, "used_s": 28800, "quota_s": 36000})]
