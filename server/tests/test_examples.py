from pathlib import Path

import yaml

from wristcall.config import parse_config

ROOT = Path(__file__).resolve().parents[2]
ENV = {"WRISTCALL_DOMAIN": "wc.example.test", "OPENAI_API_KEY": "sk-x", "CHAT_MODEL": "model-x"}


def test_root_example_config_is_valid():
    data = yaml.safe_load((ROOT / "wristcall.example.yaml").read_text(encoding="utf-8"))
    cfg = parse_config(data, ENV)
    assert cfg.server.public_url == "https://wc.example.test"
    assert set(cfg.profiles) == {"default", "demo"}
    assert cfg.profiles["demo"].stt == "demo-stt"


async def test_example_config_boots_into_two_agents():
    from wristcall.bootstrap import bootstrap
    from wristcall.storage import open_sqlite_storage

    data = yaml.safe_load((ROOT / "wristcall.example.yaml").read_text(encoding="utf-8"))
    cfg = parse_config(data, ENV)
    assert cfg.limits.max_agents_per_user == 20
    st = open_sqlite_storage(":memory:")
    await bootstrap(st, cfg)
    owner = await st.users.by_handle("owner")
    assert [a.slug for a in await st.agents.list(owner.id)] == ["default", "demo"]
