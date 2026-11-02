from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None

# timestamp columns that may still hold second-precision values written by
# older databases, upgraded to milliseconds on startup
_SECOND_PRECISION_TS_COLUMNS = (
    ("projects", "created_at"),
    ("projects", "reason_started_at"),
    ("projects", "reason_last_heartbeat_at"),
    ("facts", "created_at"),
    ("intents", "created_at"),
    ("intents", "concluded_at"),
    ("intents", "last_heartbeat_at"),
    ("hints", "created_at"),
)

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    creator TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS intent_targets (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_intent_targets_project ON intent_targets (project_id, fact_id);

CREATE INDEX IF NOT EXISTS idx_facts_project ON facts (project_id, id);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        _reject_legacy_schema(conn)
        conn.executescript(SCHEMA)
        # databases written by the version that stored project completion in the
        # projects table carry two now-unused columns; drop them so the only
        # live data is the graph, with completion derived from it on read
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
        for column in ("completed_at", "completed_by_intent"):
            if column in columns:
                conn.execute(f"ALTER TABLE projects DROP COLUMN {column}")
        # databases written before millisecond timestamps store second-precision
        # values (20 chars ending in Z); normalize them so that ordering and
        # timeout comparisons stay consistent with millisecond values
        for table, column in _SECOND_PRECISION_TS_COLUMNS:
            conn.execute(
                f"UPDATE {table} SET {column} = substr({column},1,19) || '.000Z' "
                f"WHERE {column} IS NOT NULL AND length({column}) = 20 "
                f"AND substr({column},20,1) = 'Z'"
            )


def _reject_legacy_schema(conn: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "facts" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(facts)")}
    if "created_at" not in columns:
        raise RuntimeError(
            "database uses the legacy single-goal schema; convert it first with: "
            "cairn migrate-db --db-path <old.db> --target <new.db>"
        )


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
