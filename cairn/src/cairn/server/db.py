from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Generator

from fastapi import Request

DEFAULT_DATA_FOLDER = Path.home() / ".local" / "share" / "cairn"

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15,
    dispatcher_heartbeat_timeout INTEGER NOT NULL DEFAULT 45
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout, dispatcher_heartbeat_timeout) VALUES (1, 15, 15, 45);

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
    weight INTEGER NOT NULL DEFAULT 0,
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        _ensure_current_schema(conn)
        conn.executescript(SCHEMA)


def _ensure_current_schema(conn: sqlite3.Connection) -> None:
    """Refuse databases written by older versions instead of upgrading them;
    conversions are provided by the ``cairn migrate-db`` subcommands only."""
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
            "cairn migrate-db multi-goal --db-path <old.db> --target <new.db>"
        )
    intent_columns = {row["name"] for row in conn.execute("PRAGMA table_info(intents)")}
    if "weight" not in intent_columns:
        raise RuntimeError(
            "database predates intent weights; upgrade it first with: "
            "cairn migrate-db intent-weight --db-path <old.db> --target <new.db>"
        )
    settings_columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
    if "dispatcher_heartbeat_timeout" not in settings_columns:
        raise RuntimeError(
            "database predates the dispatcher heartbeat setting; upgrade it first with: "
            "cairn migrate-db settings-heartbeat --db-path <old.db> --target <new.db>"
        )


def get_conn(request: Request) -> AbstractContextManager[sqlite3.Connection]:
    return connect(request.app.state.database_path)


@contextmanager
def connect(path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(path))
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
