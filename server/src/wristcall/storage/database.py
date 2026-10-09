"""SQLite connection shared by the server and the CLI: pragmas, FTS5 check and migrations."""

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .migrations import migrate


class DatabaseError(Exception):
    pass


def _require_fts5(conn: sqlite3.Connection) -> None:
    # The history (epic E3) uses FTS5; fail at startup rather than at the first search.
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.fts5_probe")
    except sqlite3.OperationalError as e:
        raise DatabaseError(f"this SQLite build has no FTS5 ({sqlite3.sqlite_version}): {e}") from e


def _enable_wal(conn: sqlite3.Connection, timeout_s: float = 5.0) -> None:
    # Switching a file to WAL needs a moment without other connections, and SQLite answers "locked" right away
    # instead of waiting for busy_timeout. Happens once per file, when server and CLI open a 0.1/0.2 database together.
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


class Database:
    def __init__(self, path: Path | str) -> None:
        in_memory = str(path) == ":memory:"
        if not in_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=5000")
            if not in_memory:
                _enable_wal(self._conn)
            self._conn.execute("PRAGMA foreign_keys=ON")
            _require_fts5(self._conn)
            self.version = migrate(self._conn)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Several statements as one unit: holds the connection lock and an immediate (write) transaction."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def database_path(data_dir: Path) -> Path:
    return Path(data_dir) / "wristcall.db"
