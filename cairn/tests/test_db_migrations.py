from __future__ import annotations

import sqlite3

import pytest

from cairn.server import db


def test_configure_rejects_legacy_facts_schema(tmp_path, monkeypatch) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE facts (
                id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                description TEXT NOT NULL,
                PRIMARY KEY (id, project_id)
            )
            """
        )

    monkeypatch.setattr(db, "_db_path", None)
    with pytest.raises(RuntimeError, match="migrate-db"):
        db.configure(path)


def test_configure_drops_deprecated_completion_columns_and_upgrades_precision(tmp_path, monkeypatch) -> None:
    """A database written by the previous commit (HEAD) stores completion in the
    projects table with second-precision timestamps; configure must drop the
    unused completion columns and normalize all timestamp values to milliseconds.
    """
    head_schema = """
    CREATE TABLE projects (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        completed_by_intent TEXT,
        reason_worker TEXT,
        reason_trigger TEXT,
        reason_started_at TEXT,
        reason_last_heartbeat_at TEXT
    );
    CREATE TABLE facts (
        id TEXT NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        description TEXT NOT NULL,
        created_at TEXT NOT NULL,
        creator TEXT,
        PRIMARY KEY (id, project_id)
    );
    CREATE TABLE intents (
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
    CREATE TABLE intent_sources (
        intent_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        fact_id TEXT NOT NULL,
        PRIMARY KEY (intent_id, project_id, fact_id),
        FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
    );
    CREATE TABLE intent_targets (
        intent_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        fact_id TEXT NOT NULL,
        PRIMARY KEY (intent_id, project_id, fact_id),
        FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
    );
    """

    path = tmp_path / "head.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(head_schema)
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, completed_at, completed_by_intent) "
            "VALUES ('proj_001', 'old', 'completed', '2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z', 'i001')"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, created_at) VALUES ('origin', 'proj_001', 'o', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, created_at) VALUES ('g001', 'proj_001', 'goal', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, created_at, creator) VALUES ('f001', 'proj_001', 'proof', '2026-01-01T00:05:00Z', 'reasoner')"
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) "
            "VALUES ('i001', 'proj_001', 'done', 'reasoner', 'reasoner', '2026-01-01T00:05:00Z', '2026-01-01T00:05:00Z', '2026-01-01T00:05:00Z')"
        )
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES ('i001', 'proj_001', 'origin')"
        )
        conn.execute(
            "INSERT INTO intent_targets (intent_id, project_id, fact_id) VALUES ('i001', 'proj_001', 'f001')"
        )
        conn.execute(
            "INSERT INTO intent_targets (intent_id, project_id, fact_id) VALUES ('i001', 'proj_001', 'g001')"
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with db.get_conn() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
        assert "completed_at" not in columns
        assert "completed_by_intent" not in columns
        row = conn.execute("SELECT created_at FROM projects WHERE id = 'proj_001'").fetchone()
        assert row["created_at"] == "2026-01-01T00:00:00.000Z"
        row = conn.execute("SELECT concluded_at FROM intents WHERE id = 'i001'").fetchone()
        assert row["concluded_at"] == "2026-01-01T00:05:00.000Z"

    # the migrated data is self-consistent: the completion edges survive and the
    # server derives the same completion from the graph
    from fastapi.testclient import TestClient

    from cairn.server.app import app

    with TestClient(app) as client:
        detail = client.get("/projects/proj_001").json()
        assert detail["project"]["status"] == "completed"
        assert detail["project"]["completed_at"] == "2026-01-01T00:05:00.000Z"
        assert detail["project"]["completed_by_intent"] == "i001"
