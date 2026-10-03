"""SQLite shared between the server and the CLI (devices and pairing)."""

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL,
  revoked_at REAL
);
CREATE TABLE IF NOT EXISTS pairing_codes (
  code TEXT PRIMARY KEY,
  expires_at REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  used_at REAL
);
CREATE TABLE IF NOT EXISTS pairing_requests (
  poll_hash TEXT PRIMARY KEY,
  short_id TEXT NOT NULL,
  device_name TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  status TEXT NOT NULL,
  device_id TEXT
);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        in_memory = str(path) == ":memory:"
        if not in_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not in_memory:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def open_database(data_dir: Path) -> Database:
    return Database(Path(data_dir) / "wristcall.db")
