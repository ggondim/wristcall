import asyncio
import json

import httpx
import respx
import yaml
from typer.testing import CliRunner

from wristcall import cli
from wristcall.history import KEY_ID, CallLog
from wristcall.history_codec import HistoryCodec, new_key, parse_key
from wristcall.storage import CallRecord, open_sqlite_storage

runner = CliRunner()
HOOK_URL = "https://hooks.example/in"


def write_config(tmp_path, key=None):
    data = {
        "server": {"public_url": "https://wc.example.test", "data_dir": str(tmp_path / "data")},
        "providers": {"stt": {"type": "fake_stt"}, "llm": {"type": "echo_chat"}, "tts": {"type": "tone_tts"}},
        "profiles": {"default": {"display_name": "Test", "stt": "stt", "responder": "llm", "tts": "tts"}},
    }
    if key:
        data["history"] = {"encryption_key": key}
    path = tmp_path / "wristcall.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def invoke(cfg, *args, input=None):
    return runner.invoke(cli.app, [*args, "-c", str(cfg)], input=input)


def ok(cfg, *args, input=None) -> str:
    r = invoke(cfg, *args, input=input)
    assert r.exit_code == 0, r.output
    return r.output


def seed(tmp_path, key=None, *calls):
    """Writes calls straight into the database, like the server does: (id, created_at, call_type, status, error, texts)."""

    async def main():
        st = open_sqlite_storage(tmp_path / "data")
        owner = await st.users.by_handle("owner")
        [agent] = await st.agents.list(owner.id)
        codec = HistoryCodec(parse_key(key) if key else None)
        for call_id, at, call_type, status, error, texts in calls:
            record = await st.calls.create(CallRecord(
                id=call_id, user_id=owner.id, agent_id=agent.id, device_id=None, call_type=call_type, status=status,
                error=error, attempts=3 if error else 0, created_at=at, updated_at=at, ended_at=at,
                agent_slug=agent.slug, agent_name=agent.display_name,
            ))
            log = CallLog(st, codec, record, now=lambda: at)
            for role, text in texts:
                await log.add(role, text)
        await st.close()
        return owner, agent

    return asyncio.run(main())


def db_rows(tmp_path, sql):
    st = open_sqlite_storage(tmp_path / "data")
    try:
        return [tuple(r) for r in st.db.query(sql)]
    finally:
        asyncio.run(st.close())


def test_list_search_show_and_export(tmp_path):
    cfg = write_config(tmp_path)
    ok(cfg, "users", "list")  # bootstrap: owner and the default agent
    seed(
        tmp_path, None,
        ("c_1", 1_000.0, "conversation", "ended", None, [("user", "Comprar leite"), ("agent", "Anotado.")]),
        ("c_2", 2_000.0, "conversation", "ended", None, [("user", "Reunião às 15h")]),
    )
    listed = ok(cfg, "history", "list")
    assert listed.index("c_2") < listed.index("c_1") and "Reunião às 15h" in listed
    assert "c_2" in ok(cfg, "history", "list", "--search", "REUNIAO") and "c_1" not in ok(cfg, "history", "list", "-s", "reuniao")
    assert ok(cfg, "history", "list", "--since", "1500") .count("c_") == 1
    shown = json.loads(ok(cfg, "history", "show", "c_1"))
    assert [e["text"] for e in shown["entries"]] == ["Comprar leite", "Anotado."]
    assert invoke(cfg, "history", "show", "c_nope").exit_code == 1
    md = ok(cfg, "history", "export")
    assert md.startswith("# wristcall history") and "**Agent:** Anotado." in md
    out = tmp_path / "h.json"
    ok(cfg, "history", "export", "--format", "json", "--until", "1970-01-01T00:20:00", "-o", str(out))
    assert [c["id"] for c in json.loads(out.read_text())["calls"]] == ["c_1"]
    bad = invoke(cfg, "history", "list", "--since", "ontem")
    assert bad.exit_code == 1 and "not a time" in bad.output


def test_rm_and_clear(tmp_path):
    cfg = write_config(tmp_path)
    ok(cfg, "users", "list")
    seed(tmp_path, None, *[(f"c_{i}", float(i), "conversation", "ended", None, [("user", "x")]) for i in range(3)])
    assert invoke(cfg, "history", "rm", "c_0", input="n\n").exit_code == 1
    ok(cfg, "history", "rm", "c_0", "--yes")
    assert invoke(cfg, "history", "clear", "--yes").exit_code == 2  # neither --agent nor --all
    assert "Deleted 2 call(s)." in ok(cfg, "history", "clear", "--agent", "default", "--yes")
    assert ok(cfg, "history", "list").strip() == "No calls."


def test_redeliver_from_the_cli(tmp_path):
    cfg = write_config(tmp_path)
    ok(cfg, "users", "list")
    ok(cfg, "agents", "add", "note", "--call-type", "one-shot", "--action", json.dumps({"type": "webhook", "url": HOOK_URL}))

    async def failed():
        st = open_sqlite_storage(tmp_path / "data")
        owner = await st.users.by_handle("owner")
        note = [a for a in await st.agents.list(owner.id) if a.slug == "note"][0]
        record = await st.calls.create(CallRecord(
            id="c_f", user_id=owner.id, agent_id=note.id, device_id=None, call_type="one-shot", status="failed",
            error="delivery_failed", attempts=3, created_at=1.0, updated_at=1.0, ended_at=2.0,
        ))
        await CallLog(st, HistoryCodec(), record).add("user", "comprar leite")
        await st.close()

    asyncio.run(failed())
    with respx.mock(assert_all_called=False) as mock:
        hook = mock.post(HOOK_URL).mock(return_value=httpx.Response(204))
        out = ok(cfg, "history", "redeliver", "c_f")
    assert "Delivered (HTTP 204, 4 attempt(s) in all)." in out
    assert hook.calls.last.request.headers["idempotency-key"] == "c_f"
    again = invoke(cfg, "history", "redeliver", "c_f")
    assert again.exit_code == 1 and "can be delivered again" in again.output


def test_encrypt_then_decrypt(tmp_path):
    plain = write_config(tmp_path)
    ok(plain, "users", "list")
    seed(tmp_path, None, ("c_1", 1.0, "conversation", "ended", None, [("user", "cofre azul"), ("agent", "ok")]))
    printed = runner.invoke(cli.app, ["history", "new-key"]).output.strip()
    assert len(printed) == 43 and len(parse_key(printed)) == 32
    key = new_key()
    locked = write_config(tmp_path, key)
    assert "Encrypted 2 utterance(s)." in ok(locked, "history", "encrypt", "--yes")
    rows = db_rows(tmp_path, "SELECT text, sealed FROM call_entries ORDER BY seq")
    assert all(sealed == 1 and "cofre" not in text for text, sealed in rows)
    assert db_rows(tmp_path, f"SELECT key FROM meta WHERE key = '{KEY_ID}'") == [(KEY_ID,)]
    assert "c_1" in ok(locked, "history", "list", "--search", "cofre")
    # Without the key (or with another), the history commands refuse instead of showing nothing.
    no_key = write_config(tmp_path)
    refused = invoke(no_key, "history", "list")
    assert refused.exit_code == 1 and "encrypted" in refused.output
    other = write_config(tmp_path, new_key())
    assert invoke(other, "history", "list").exit_code == 1
    locked = write_config(tmp_path, key)
    assert "Decrypted 2 utterance(s)." in ok(locked, "history", "decrypt", "--yes")
    assert db_rows(tmp_path, "SELECT text, sealed FROM call_entries ORDER BY seq") == [("cofre azul", 0), ("ok", 0)]
    plain = write_config(tmp_path)
    assert "c_1" in ok(plain, "history", "list", "--search", "azul")


def test_encrypt_needs_a_key(tmp_path):
    cfg = write_config(tmp_path)
    r = invoke(cfg, "history", "encrypt", "--yes")
    assert r.exit_code == 1 and "history.encryption_key" in r.output
