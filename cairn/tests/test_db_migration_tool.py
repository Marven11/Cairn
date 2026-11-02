from __future__ import annotations

import sqlite3

import pytest

from cairn.server import db
from cairn.server.migrate import migrate_database

LEGACY_SCHEMA = """
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
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
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


def _build_legacy_database(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    with conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at) VALUES ('proj_001', 'active-one', 'active', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at) VALUES ('proj_002', 'done', 'completed', '2026-01-02T00:00:00Z')"
        )
        for pid in ("proj_001", "proj_002"):
            conn.execute(
                "INSERT INTO facts (id, project_id, description) VALUES ('origin', ?, 'start')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO facts (id, project_id, description) VALUES ('goal', ?, 'finish')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) "
                "VALUES ('i001', ?, 'f001', 'explore', 'reasoner', 'explorer', '2026-01-01T00:01:00Z', '2026-01-01T00:00:30Z', '2026-01-01T00:01:00Z')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES ('i001', ?, 'origin')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO facts (id, project_id, description) VALUES ('f001', ?, 'first finding')",
                (pid,),
            )
        # proj_002 additionally has the completion intent pointing at the legacy goal fact
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) "
            "VALUES ('i002', 'proj_002', 'goal', 'solved', 'reasoner', 'reasoner', '2026-01-02T00:02:00Z', '2026-01-02T00:01:30Z', '2026-01-02T00:02:00Z')"
        )
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES ('i002', 'proj_002', 'f001')"
        )
        conn.execute(
            "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES ('h001', 'proj_001', 'clue', 'human', '2026-01-01T00:00:10Z')"
        )
        conn.execute("INSERT INTO scoped_counters (project_id, kind, value) VALUES ('proj_001', 'fact', 1)")
    conn.close()


def test_migrate_database_converts_goals_and_targets(tmp_path, monkeypatch) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "migrated.db"
    _build_legacy_database(source)

    report = migrate_database(source, target)

    assert report.projects == 2
    assert report.goals_renamed == 2
    assert report.placeholder_facts == 1
    assert report.intent_targets == 4

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(target)
    with db.get_conn() as conn:
        proj1_facts = conn.execute(
            "SELECT id, created_at, creator FROM facts WHERE project_id = 'proj_001' ORDER BY created_at, rowid"
        ).fetchall()
        assert [(row["id"]) for row in proj1_facts] == ["origin", "g001", "f001"]
        # origin and the goal inherit the project creation time; f001 inherits the conclude time;
        # configure normalizes second-precision timestamps to millisecond precision
        assert proj1_facts[0]["created_at"] == "2026-01-01T00:00:00.000Z"
        assert proj1_facts[1]["created_at"] == "2026-01-01T00:00:00.000Z"
        assert proj1_facts[2]["created_at"] == "2026-01-01T00:01:00.000Z"
        # creator is derived from the producing intent's worker
        assert proj1_facts[0]["creator"] is None
        assert proj1_facts[1]["creator"] is None
        assert proj1_facts[2]["creator"] == "explorer"

        migrated_goal = conn.execute(
            "SELECT created_at FROM facts WHERE project_id = 'proj_002' AND id = 'g001'"
        ).fetchone()
        # the legacy goal keeps the project creation time, not the completion time
        assert migrated_goal["created_at"] == "2026-01-02T00:00:00.000Z"

        targets = conn.execute(
            "SELECT fact_id FROM intent_targets WHERE project_id = 'proj_002' AND intent_id = 'i002' ORDER BY rowid"
        ).fetchall()
        # the legacy completion edge gets a placeholder conclusion fact first, then the goal
        assert [row["fact_id"] for row in targets] == ["f002", "g001"]
        placeholder = conn.execute(
            "SELECT description, created_at, creator FROM facts WHERE project_id = 'proj_002' AND id = 'f002'"
        ).fetchone()
        assert placeholder is not None
        assert placeholder["created_at"] == "2026-01-02T00:02:00.000Z"
        assert placeholder["creator"] == "reasoner"

        goal_counter = conn.execute(
            "SELECT value FROM scoped_counters WHERE project_id = 'proj_001' AND kind = 'goal'"
        ).fetchone()
        assert goal_counter["value"] == 1
        fact_counter = conn.execute(
            "SELECT value FROM scoped_counters WHERE project_id = 'proj_002' AND kind = 'fact'"
        ).fetchone()
        assert fact_counter["value"] == 2

        statuses = conn.execute("SELECT id, status FROM projects ORDER BY id").fetchall()
        assert [(row["id"], row["status"]) for row in statuses] == [
            ("proj_001", "active"),
            ("proj_002", "completed"),
        ]

        hint = conn.execute("SELECT id FROM hints WHERE project_id = 'proj_001'").fetchone()
        assert hint["id"] == "h001"


def test_migrated_database_accepts_new_writes(tmp_path, monkeypatch) -> None:
    """Id counters are rebuilt from migrated rows, so creating a new project,
    intent or goal after the migration must not collide with migrated ids.
    """
    from fastapi.testclient import TestClient

    from cairn.server.app import app

    source = tmp_path / "legacy.db"
    target = tmp_path / "migrated.db"
    _build_legacy_database(source)
    report = migrate_database(source, target)
    assert report.projects == 2

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(target)
    with TestClient(app) as client:
        response = client.post(
            "/projects",
            json={"title": "after", "origin": "o", "goal": "g"},
        )
        assert response.status_code == 201
        assert response.json()["project"]["id"] != "proj_001"
        assert response.json()["project"]["id"] != "proj_002"

        response = client.post(
            "/projects/proj_001/intents",
            json={"from": ["f001"], "description": "after", "creator": "c", "worker": None},
        )
        assert response.status_code == 201
        assert response.json()["id"] != "i001"

        response = client.post(
            "/projects/proj_001/goals",
            json={"description": "after", "creator": "c"},
        )
        assert response.status_code == 201
        assert response.json()["fact"]["id"] == "g002"


def test_migrate_database_rejects_existing_target(tmp_path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "migrated.db"
    _build_legacy_database(source)
    target.write_text("existing", encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        migrate_database(source, target)


def test_migrate_database_rejects_same_path(tmp_path) -> None:
    source = tmp_path / "legacy.db"
    _build_legacy_database(source)

    with pytest.raises(ValueError, match="must differ"):
        migrate_database(source, source)


def test_migrate_database_rejects_current_schema_source_and_cleans_target(tmp_path) -> None:
    source = tmp_path / "modern.db"
    target = tmp_path / "out.db"
    with sqlite3.connect(source) as conn:
        conn.executescript(db.SCHEMA)

    with pytest.raises(ValueError, match="multi-goal schema"):
        migrate_database(source, target)
    assert not target.exists()
