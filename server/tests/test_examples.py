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


def test_root_example_central_account_block_is_valid():
    # The block ships commented out: uncommented as is, it must parse.
    lines = (ROOT / "wristcall.example.yaml").read_text(encoding="utf-8").splitlines()
    start = lines.index("# central_account:")
    block = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        block.append(line[2:])
    data = yaml.safe_load((ROOT / "wristcall.example.yaml").read_text(encoding="utf-8"))
    central = yaml.safe_load("\n".join(block))
    # Placeholders ("<watch client id>") stand for real ids.
    central["central_account"]["clients"] = [f"client-{i}" for i, _ in enumerate(central["central_account"]["clients"])]
    data.update(central)
    central = parse_config(data, ENV).central_account
    assert central.issuer == "https://cloud.wristcall.example"
    assert central.audience == ["https://wristcall.example.com"]
    assert central.device_credential == "approval"
