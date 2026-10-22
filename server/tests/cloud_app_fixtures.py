"""Builds a cloud-mode app (`server.mode: cloud`) wired to the in-process FakeCloud, for the cloud-mode app tests."""

import copy
import time
from typing import Any

from conftest import fake_resolver
from fake_cloud import CLOUD_URL, FakeCloud
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wristcall.app import create_app
from wristcall.config import AppConfig, parse_config

NODE_URL = "https://node.test"
NODE_SECRET = "wc_node_" + "n" * 43
HISTORY_KEY = "A" * 43
SEAL_KEY = "B" * 43
# The users' own endpoints in the tests resolve to public addresses (limits.public_only is forced in cloud mode).
PUBLIC_HOSTS = {h: ["93.184.216.34"] for h in ("stt.example.test", "tts.example.test", "llm.example.test")}


def cloud_data(**over: Any) -> dict[str, Any]:
    """The smallest cloud-mode YAML (as a dict); `over` replaces top-level sections, None removes one."""
    data: dict[str, Any] = {
        "server": {"public_url": NODE_URL, "mode": "cloud"},
        "providers": {
            "stt": {"type": "openai_stt", "base_url": "https://stt.example.test/v1", "model": "w"},
            "tts": {"type": "openai_tts", "base_url": "https://tts.example.test/v1", "model": "t", "voice": "alloy"},
        },
        "central_account": {"issuer": CLOUD_URL, "audience": [NODE_URL]},
        "history": {"encryption_key": HISTORY_KEY},
        # A fast heartbeat: the node is ready right after the first beat, and again soon after the Cloud is back.
        "cloud_node": {
            "cloud_url": CLOUD_URL, "node_id": "node-1", "node_secret": NODE_SECRET, "seal_key": SEAL_KEY,
            "heartbeat_s": 0.05,
        },
    }
    for key, value in over.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = copy.deepcopy(value)
    return data


def cloud_config(**over: Any) -> AppConfig:
    return parse_config(cloud_data(**over), {})


def new_fake() -> FakeCloud:
    return FakeCloud(NODE_URL, NODE_SECRET)


def cloud_app(fake: FakeCloud, config: AppConfig | None = None, **kwargs: Any) -> FastAPI:
    """A cloud-mode app over `fake`, with short shutdown waits (I13): leaving a TestClient with the Cloud down does
    not wait the production 20 s + 8 s."""
    options: dict[str, Any] = {
        "cloud_http": fake.http(), "resolver": fake_resolver(PUBLIC_HOSTS),
        "cloud_stop_s": 0.2, "background_grace_s": 0.2, "outbox_max_delay_s": 0.05,
    }
    options.update(kwargs)
    return create_app(config or cloud_config(), **options)


def bearer(fake: FakeCloud, account: str = "acc-1", device: str = "dev-ios", platform: str = "ios", **claims: Any
           ) -> dict[str, str]:
    return {"Authorization": f"Bearer {fake.user_token(account, device, platform, **claims)}"}


def wait_ready(client: TestClient, timeout_s: float = 3.0) -> dict[str, Any]:
    """Waits for the node's first accepted heartbeat (`/v1/health` 200) and returns the health body."""
    deadline = time.monotonic() + timeout_s
    while True:
        r = client.get("/v1/health")
        if r.status_code == 200:
            return r.json()
        if time.monotonic() > deadline:
            raise AssertionError(f"node not ready: {r.status_code} {r.text}")
        time.sleep(0.02)
