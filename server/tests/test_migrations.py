import sqlite3
import threading

import pytest

from wristcall.storage import database as database_module
from wristcall.storage.database import Database, DatabaseError, database_path
from wristcall.storage.migrations import LATEST, MIGRATIONS, MigrationError

# Schema exactly as server 0.2.0 created it (store.py SCHEMA), to prove adoption of existing databases.
SCHEMA_0_2_0 = """
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, revoked_at REAL
);
CREATE TABLE IF NOT EXISTS pairing_codes (
  code TEXT PRIMARY KEY, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, used_at REAL
);
CREATE TABLE IF NOT EXISTS pairing_requests (
  poll_hash TEXT PRIMARY KEY, short_id TEXT NOT NULL, device_name TEXT NOT NULL, created_at REAL NOT NULL,
  expires_at REAL NOT NULL, status TEXT NOT NULL, device_id TEXT
);
"""


def tables(db: Database) -> set[str]:
    return {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}


def columns(db: Database, table: str) -> set[str]:
    return {r["name"] for r in db.query(f"PRAGMA table_info({table})")}


def legacy_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_0_2_0)
    conn.execute("INSERT INTO devices VALUES ('d1', 'Watch', 'h1', 1.0, NULL)")
    conn.execute("INSERT INTO pairing_codes (code, expires_at) VALUES ('12345678', 9e9)")
    conn.execute("INSERT INTO pairing_requests VALUES ('p1', '0001', 'Watch 2', 1.0, 9e9, 'pending', NULL)")
    conn.commit()
    conn.close()


def test_fresh_database_is_at_latest_version(tmp_path):
    db = Database(database_path(tmp_path))
    assert db.version == LATEST
    assert db.query("PRAGMA user_version")[0][0] == LATEST
    assert {"devices", "pairing_codes", "pairing_requests", "users", "api_tokens", "agents", "meta", "calls"} <= tables(db)
    assert "user_id" in columns(db, "devices") and "user_id" in columns(db, "pairing_codes")
    assert "central_subject" in columns(db, "users") and "target_user_id" in columns(db, "pairing_requests")
    assert db.query("PRAGMA foreign_keys")[0][0] == 1


def test_in_memory_database_migrates():
    assert Database(":memory:").version == LATEST


def test_legacy_database_is_adopted_and_keeps_devices(tmp_path):
    path = database_path(tmp_path)
    legacy_db(path)
    db = Database(path)
    assert db.version == LATEST
    rows = db.query("SELECT id, name, token_hash, user_id FROM devices")
    assert [tuple(r) for r in rows] == [("d1", "Watch", "h1", None)]
    assert db.query("SELECT short_id, user_id FROM pairing_requests")[0]["short_id"] == "0001"
    # Codes from 0.2.0 have no owner: dropped.
    assert db.query("SELECT COUNT(*) FROM pairing_codes")[0][0] == 0


def test_reopening_does_not_reapply(tmp_path):
    path = database_path(tmp_path)
    Database(path).execute("INSERT INTO users (id, handle, display_name, created_at) VALUES ('u1', 'owner', 'Owner', 1.0)")
    db = Database(path)
    assert db.version == LATEST
    assert db.query("SELECT handle FROM users")[0]["handle"] == "owner"


def test_newer_schema_is_refused(tmp_path):
    path = database_path(tmp_path)
    Database(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {LATEST + 1}")
    conn.close()
    with pytest.raises(MigrationError, match="newer"):
        Database(path)


def test_failed_step_rolls_back(tmp_path, monkeypatch):
    from wristcall.storage import migrations

    path = database_path(tmp_path)
    legacy_db(path)
    broken = [migrations.MIGRATIONS[0], migrations.MIGRATIONS[1] + ["THIS IS NOT SQL"]]
    monkeypatch.setattr(migrations, "MIGRATIONS", broken)
    monkeypatch.setattr(migrations, "LATEST", len(broken))
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'users'").fetchall() == []
    conn.close()


def test_concurrent_openers_migrate_once(tmp_path):
    path = database_path(tmp_path)
    legacy_db(path)
    errors: list[BaseException] = []

    def open_it() -> None:
        try:
            Database(path).close()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=open_it) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert Database(path).version == LATEST


def test_wal_switch_waits_for_other_connections(tmp_path):
    path = database_path(tmp_path)
    legacy_db(path)
    other = sqlite3.connect(path, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")  # holds the write lock, as a second opener would
    threading.Timer(0.3, other.rollback).start()
    db = Database(path)  # without the retry: "database is locked" right away
    assert db.query("PRAGMA journal_mode")[0][0] == "wal"
    other.close()


def test_server_0_2_0_statements_still_work_after_migration(tmp_path):
    # Rollback to 0.2.0 without restoring the file: its INSERTs and SELECTs must keep working.
    db = Database(database_path(tmp_path))
    db.execute("INSERT INTO pairing_codes (code, expires_at) VALUES (?, ?)", ("87654321", 9e9))
    db.execute("INSERT INTO devices (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)", ("d2", "W", "h2", 1.0))
    db.execute(
        "INSERT INTO pairing_requests (poll_hash, short_id, device_name, created_at, expires_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'pending')",
        ("p2", "0002", "W", 1.0, 9e9),
    )
    assert db.query("SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL", ("h2",))[0]["id"] == "d2"


def test_missing_fts5_is_a_clear_error(monkeypatch):
    def no_fts5(conn):
        raise DatabaseError("this SQLite build has no FTS5")

    monkeypatch.setattr(database_module, "_require_fts5", no_fts5)
    with pytest.raises(DatabaseError, match="FTS5"):
        Database(":memory:")


def test_fts5_is_available_here():
    # CI runs this on the same Python as the Docker image; the image itself is checked by the CI smoke step.
    conn = sqlite3.connect(":memory:")
    database_module._require_fts5(conn)


def test_rollback_to_0_3_0_and_upgrade_again_adopts_the_calls_table(tmp_path):
    # Rollback to 0.3.0 (which refuses a newer schema): the operator restores a version 3 database (the schema of
    # 0.4.0, before the central account columns) and sets user_version back to 2, keeping the calls table.
    # The next upgrade must adopt it with its rows.
    path = database_path(tmp_path)
    conn = sqlite3.connect(path, isolation_level=None)
    for step in MIGRATIONS[:3]:
        for statement in step:
            conn.execute(statement)
    conn.execute("INSERT INTO users (id, handle, display_name, created_at) VALUES ('u1', 'owner', 'Owner', 1.0)")
    conn.execute(
        "INSERT INTO calls (id, user_id, agent_id, call_type, status, created_at, updated_at) "
        "VALUES ('c1', 'u1', 'ag1', 'one-shot', 'delivered', 1.0, 1.0)"
    )
    conn.execute("PRAGMA user_version = 2")
    conn.close()
    again = Database(path)
    assert again.version == LATEST
    assert [r["id"] for r in again.query("SELECT id FROM calls")] == ["c1"]


def test_version_3_database_gains_central_columns(tmp_path):
    # A 0.4.0 database (version 3) upgrades in place and keeps its rows.
    path = database_path(tmp_path)
    conn = sqlite3.connect(path, isolation_level=None)
    for step in MIGRATIONS[:3]:
        for statement in step:
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 3")
    conn.execute("INSERT INTO users VALUES ('u_000000000001', 'owner', 'Owner', 1.0)")
    conn.close()
    db = Database(path)
    assert db.version == LATEST
    assert "central_subject" in columns(db, "users")
    assert "target_user_id" in columns(db, "pairing_requests")
    assert db.query("SELECT handle, central_subject FROM users")[0]["handle"] == "owner"
    assert db.query("SELECT central_subject FROM users")[0]["central_subject"] is None
    db.close()


def test_version_4_database_moves_call_text_to_the_history(tmp_path):
    # A database of the E5 main (version 4) with E2 calls: their text becomes a searchable user entry.
    path = database_path(tmp_path)
    conn = sqlite3.connect(path, isolation_level=None)
    for step in MIGRATIONS[:4]:
        for statement in step:
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 4")
    conn.execute("INSERT INTO users (id, handle, display_name, created_at) VALUES ('u1', 'owner', 'Owner', 1.0)")
    conn.execute(
        "INSERT INTO agents (id, user_id, slug, display_name, icon, call_type, position, spec, created_at, updated_at) "
        "VALUES ('ag1', 'u1', 'note', 'Note', 'waveform', 'one-shot', 0, '{}', 1.0, 1.0)"
    )
    for cid, status, error, text in (
        ("c1", "delivered", None, "Reunião às 15h"),
        ("c2", "failed", "stt_failed", "começo"),
        ("c3", "empty", None, None),
    ):
        conn.execute(
            "INSERT INTO calls (id, user_id, agent_id, call_type, status, error, text, created_at, ended_at, updated_at) "
            "VALUES (?, 'u1', ?, 'one-shot', ?, ?, ?, 1.0, 2.0, 2.0)",
            (cid, "ag_deleted" if cid == "c3" else "ag1", status, error, text),
        )
    conn.close()
    db = Database(path)
    assert db.version == LATEST
    assert {"call_entries", "history_fts"} <= tables(db)
    assert db.query("SELECT COUNT(*) FROM calls WHERE text IS NOT NULL")[0][0] == 0
    entries = [tuple(r) for r in db.query("SELECT call_id, seq, role, text, sealed, error, at FROM call_entries ORDER BY call_id")]
    assert entries == [("c1", 0, "user", "Reunião às 15h", 0, None, 2.0), ("c2", 0, "user", "começo", 0, "stt_failed", 2.0)]
    agents = [tuple(r) for r in db.query("SELECT id, agent_slug, agent_name, expires_at FROM calls ORDER BY id")]
    assert agents == [("c1", "note", "Note", None), ("c2", "note", "Note", None), ("c3", "", "", None)]
    # Moved text is found with the words history_codec computes for new text.
    from wristcall.history_codec import HistoryCodec
    from wristcall.storage.sqlite import fts_query
    query = fts_query(HistoryCodec().query_terms("REUNIAO as"))
    assert [r[0] for r in db.query("SELECT rowid FROM history_fts WHERE history_fts MATCH ?", (query,))] == [1]
    # Cut exactly like new text (history_codec.words), not by the FTS5 tokenizer: "nº" is "no", "1ª" is "1a".
    assert db.query("SELECT terms FROM history_fts WHERE rowid = 1")[0][0] == "reuniao as 15h"
    db.close()


def test_step_5_database_gains_push_targets(tmp_path):
    # A database of the 0.5.0 server (version 5) upgrades in place, keeps its rows and gets the push key table.
    path = database_path(tmp_path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.create_function("wristcall_terms", 1, database_module._terms)  # step 5 calls it
    for step in MIGRATIONS[:5]:
        for statement in step:
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 5")
    conn.execute("INSERT INTO users (id, handle, display_name, created_at) VALUES ('u1', 'owner', 'Owner', 1.0)")
    conn.close()
    db = Database(path)
    assert db.version == LATEST
    assert "push_targets" in tables(db)
    assert columns(db, "push_targets") == {"id", "user_id", "device_id", "token_id", "push_key", "created_at"}
    assert [r["handle"] for r in db.query("SELECT handle FROM users")] == ["owner"]
    db.close()


def test_step_5_cuts_moved_text_like_new_text(tmp_path):
    path = database_path(tmp_path)
    conn = sqlite3.connect(path, isolation_level=None)
    for step in MIGRATIONS[:4]:
        for statement in step:
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 4")
    conn.execute("INSERT INTO users (id, handle, display_name, created_at) VALUES ('u1', 'owner', 'Owner', 1.0)")
    conn.execute(
        "INSERT INTO calls (id, user_id, agent_id, call_type, status, text, created_at, updated_at) "
        "VALUES ('c1', 'u1', 'ag1', 'one-shot', 'delivered', 'Pedido nº 42, 1ª reunião na Straße', 1.0, 1.0)"
    )
    conn.close()
    from wristcall.history_codec import HistoryCodec, words
    from wristcall.storage.sqlite import fts_query

    db = Database(path)
    assert db.query("SELECT terms FROM history_fts")[0][0] == " ".join(words("Pedido nº 42, 1ª reunião na Straße"))
    for query in ("nº 42", "1ª", "strasse"):
        match = fts_query(HistoryCodec().query_terms(query))
        assert db.query("SELECT COUNT(*) FROM history_fts WHERE history_fts MATCH ?", (match,))[0][0] == 1, query
    db.close()


def test_step_5_leaves_no_old_call_text_in_the_files(tmp_path):
    # Long text (overflow pages) moved out of calls.text must not stay readable in free pages after the migration
    # and a delete: the migration runs with secure_delete on and compacts the file.
    path = database_path(tmp_path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    for step in MIGRATIONS[:4]:
        for statement in step:
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 4")
    conn.execute("INSERT INTO users (id, handle, display_name, created_at) VALUES ('u1', 'owner', 'Owner', 1.0)")
    conn.execute(
        "INSERT INTO calls (id, user_id, agent_id, call_type, status, text, created_at, updated_at) "
        "VALUES ('c1', 'u1', 'ag1', 'one-shot', 'delivered', ?, 1.0, 1.0)",
        ("zebrasecret in the old text " * 400,),
    )
    conn.close()
    db = Database(path)
    db.execute("DELETE FROM calls")
    db.query("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert b"zebrasecret" not in raw
    db.close()
