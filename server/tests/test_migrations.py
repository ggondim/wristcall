import sqlite3
import threading

import pytest

from wristcall.storage import database as database_module
from wristcall.storage.database import Database, DatabaseError, database_path
from wristcall.storage.migrations import LATEST, MigrationError

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
    assert db.version == LATEST == 3
    assert db.query("PRAGMA user_version")[0][0] == 3
    assert {"devices", "pairing_codes", "pairing_requests", "users", "api_tokens", "agents", "meta", "calls"} <= tables(db)
    assert "user_id" in columns(db, "devices") and "user_id" in columns(db, "pairing_codes")
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
    Database(path).execute("INSERT INTO users VALUES ('u1', 'owner', 'Owner', 1.0)")
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
    # Rollback to 0.3.0 (which refuses a newer schema): the operator sets user_version back to 2 and keeps
    # the calls table. The next upgrade must adopt it with its rows.
    path = database_path(tmp_path)
    db = Database(path)
    db.execute("INSERT INTO users (id, handle, display_name, created_at) VALUES ('u1', 'owner', 'Owner', 1.0)")
    db.execute(
        "INSERT INTO calls (id, user_id, agent_id, call_type, status, created_at, updated_at) "
        "VALUES ('c1', 'u1', 'ag1', 'one-shot', 'delivered', 1.0, 1.0)"
    )
    db.execute("PRAGMA user_version = 2")
    db.close()
    again = Database(path)
    assert again.version == 3
    assert [r["id"] for r in again.query("SELECT id FROM calls")] == ["c1"]
