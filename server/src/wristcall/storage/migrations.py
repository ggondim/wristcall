"""Versioned schema of the SQLite store. The version lives in PRAGMA user_version."""

import sqlite3

# MIGRATIONS[i] takes the schema from version i to version i + 1. Never edit a released step: append a new one.
# Each step is a list of single statements (executescript would commit the surrounding transaction).
MIGRATIONS: list[list[str]] = [
    # 1: schema of server 0.1.x/0.2.0. IF NOT EXISTS adopts their databases, which are at user_version 0.
    [
        """CREATE TABLE IF NOT EXISTS devices (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          created_at REAL NOT NULL,
          revoked_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS pairing_codes (
          code TEXT PRIMARY KEY,
          expires_at REAL NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          used_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS pairing_requests (
          poll_hash TEXT PRIMARY KEY,
          short_id TEXT NOT NULL,
          device_name TEXT NOT NULL,
          created_at REAL NOT NULL,
          expires_at REAL NOT NULL,
          status TEXT NOT NULL,
          device_id TEXT
        )""",
    ],
    # 2: users, API tokens, agents and a key/value table; devices and pairing belong to a user.
    [
        """CREATE TABLE users (
          id TEXT PRIMARY KEY,
          handle TEXT NOT NULL UNIQUE,
          display_name TEXT NOT NULL,
          created_at REAL NOT NULL
        )""",
        """CREATE TABLE api_tokens (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          created_at REAL NOT NULL,
          last_used_at REAL,
          revoked_at REAL
        )""",
        """CREATE TABLE agents (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          slug TEXT NOT NULL,
          display_name TEXT NOT NULL,
          icon TEXT NOT NULL,
          call_type TEXT NOT NULL CHECK (call_type IN ('conversation', 'one-shot', 'monologue')),
          position INTEGER NOT NULL,
          spec TEXT NOT NULL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          UNIQUE (user_id, slug)
        )""",
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        # New columns are nullable so that server 0.2.0 still runs on a migrated database (rollback without restore).
        "ALTER TABLE devices ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE",
        "ALTER TABLE pairing_requests ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE",
        "ALTER TABLE pairing_codes ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE",
        # Codes issued by 0.2.0 have no owner and live 10 minutes: drop them.
        "DELETE FROM pairing_codes",
        "CREATE INDEX devices_user ON devices (user_id)",
        "CREATE INDEX api_tokens_user ON api_tokens (user_id)",
        "CREATE INDEX agents_user ON agents (user_id, position)",
    ],
    # 3: calls (one-shot and monologue in E2; the history of epic E3 grows from here). No CHECK on status or
    # call_type: SQLite cannot alter a CHECK, and the values are validated above the storage.
    # IF NOT EXISTS: a rollback to 0.3.0 sets user_version back to 2 and keeps the table; the next upgrade adopts it.
    [
        """CREATE TABLE IF NOT EXISTS calls (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL,
          device_id TEXT,
          call_type TEXT NOT NULL,
          status TEXT NOT NULL,
          error TEXT,
          text TEXT,
          attempts INTEGER NOT NULL DEFAULT 0,
          last_http_status INTEGER,
          created_at REAL NOT NULL,
          ended_at REAL,
          finished_at REAL,
          updated_at REAL NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS calls_user ON calls (user_id, created_at)",
    ],
]

LATEST = len(MIGRATIONS)


class MigrationError(Exception):
    pass


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """Applies the pending steps in one transaction and returns the final version.

    The connection must be in autocommit mode (isolation_level=None). BEGIN IMMEDIATE serializes
    concurrent openers (server and CLI on the same file): the second one waits and then finds nothing to do.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = schema_version(conn)
        if version > LATEST:
            raise MigrationError(f"database schema {version} is newer than this server ({LATEST}); upgrade the server")
        for step in MIGRATIONS[version:]:
            for statement in step:
                conn.execute(statement)
        if version < LATEST:
            conn.execute(f"PRAGMA user_version = {LATEST}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return LATEST
