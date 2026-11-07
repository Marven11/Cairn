from __future__ import annotations

import sqlite3

import pytest
from click.testing import CliRunner

from cairn.cli import main
from cairn.server import db
from cairn.server.migrate import (
    migrate_database,
    migrate_intent_weight,
    migrate_settings_heartbeat,
)

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


def test_migrate_database_converts_goals_and_targets(tmp_path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "migrated.db"
    _build_legacy_database(source)

    report = migrate_database(source, target)

    assert report.projects == 2
    assert report.goals_renamed == 2
    assert report.placeholder_facts == 1
    assert report.intent_targets == 4

    db.configure(target)
    with db.connect(target) as conn:
        proj1_facts = conn.execute(
            "SELECT id, created_at, creator FROM facts WHERE project_id = 'proj_001' ORDER BY created_at, rowid"
        ).fetchall()
        assert [(row["id"]) for row in proj1_facts] == ["origin", "g001", "f001"]
        # origin and the goal inherit the project creation time; f001 inherits the conclude time;
        # the migration tool normalizes second-precision timestamps to millisecond precision
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


def test_migrated_database_accepts_new_writes(tmp_path) -> None:
    """Id counters are rebuilt from migrated rows, so creating a new project,
    intent or goal after the migration must not collide with migrated ids.
    """
    from conftest import TEST_AUTH, basic_headers, dispatcher_login
    from fastapi.testclient import TestClient

    from cairn.server.app import create_app

    source = tmp_path / "legacy.db"
    target = tmp_path / "cairn.db"
    _build_legacy_database(source)
    report = migrate_database(source, target)
    assert report.projects == 2

    with TestClient(
        create_app(tmp_path, auth=TEST_AUTH),
        headers=basic_headers(TEST_AUTH.username, TEST_AUTH.password),
    ) as client:
        dispatcher_login(client)
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


MULTI_GOAL_HEAD_SCHEMA = """
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
CREATE TABLE hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);
CREATE TABLE counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE scoped_counters (
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15
);
INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);
"""


def _build_multi_goal_head_database(path) -> None:
    """A database written by the multi-goal version before intent weights:
    project completion still lives in the projects table and timestamps hold
    second-precision values."""
    conn = sqlite3.connect(path)
    conn.executescript(MULTI_GOAL_HEAD_SCHEMA)
    with conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, completed_at, completed_by_intent) "
            "VALUES ('proj_001', 'old', 'completed', '2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z', 'i001')"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, created_at) VALUES ('origin', 'proj_001', 'start', '2026-01-01T00:00:00Z')"
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
    conn.close()


def test_migrate_intent_weight_adds_column_and_keeps_data(tmp_path) -> None:
    source = tmp_path / "head.db"
    target = tmp_path / "cairn.db"
    _build_multi_goal_head_database(source)

    report = migrate_intent_weight(source, target)

    assert report.projects == 1
    assert report.intents == 1

    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        intent_columns = {row[1] for row in conn.execute("PRAGMA table_info(intents)")}
        assert "weight" in intent_columns
        project_columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
        assert "completed_at" not in project_columns
        assert "completed_by_intent" not in project_columns
        row = conn.execute("SELECT description, weight FROM intents WHERE id = 'i001'").fetchone()
        assert row["description"] == "done"
        assert row["weight"] == 0
        row = conn.execute("SELECT concluded_at FROM intents WHERE id = 'i001'").fetchone()
        assert row["concluded_at"] == "2026-01-01T00:05:00.000Z"
        row = conn.execute("SELECT description FROM facts WHERE id = 'origin'").fetchone()
        assert row["description"] == "start"
    finally:
        conn.close()

    final = tmp_path / "final.db"
    followup = migrate_settings_heartbeat(target, final)
    assert followup.settings == 1
    db.configure(final)
    with db.connect(final) as conn:
        row = conn.execute("SELECT weight FROM intents WHERE id = 'i001'").fetchone()
        assert row["weight"] == 0
        row = conn.execute("SELECT dispatcher_heartbeat_timeout FROM settings WHERE rowid = 1").fetchone()
        assert row["dispatcher_heartbeat_timeout"] == 45


def test_migrate_intent_weight_rejects_wrong_sources(tmp_path) -> None:
    legacy = tmp_path / "legacy.db"
    _build_legacy_database(legacy)
    with pytest.raises(ValueError):
        migrate_intent_weight(legacy, tmp_path / "out.db")

    current = tmp_path / "current.db"
    with sqlite3.connect(current) as conn:
        conn.executescript(db.SCHEMA)
    with pytest.raises(ValueError):
        migrate_intent_weight(current, tmp_path / "out.db")
    assert not (tmp_path / "out.db").exists()


def test_migrate_intent_weight_rejects_same_and_existing_target(tmp_path) -> None:
    source = tmp_path / "head.db"
    _build_multi_goal_head_database(source)

    with pytest.raises(ValueError):
        migrate_intent_weight(source, source)

    target = tmp_path / "out.db"
    target.write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError):
        migrate_intent_weight(source, target)


def _build_pre_heartbeat_database(path) -> None:
    """The current schema minus the dispatcher heartbeat column."""
    with sqlite3.connect(path) as conn:
        conn.executescript(db.SCHEMA)
        conn.execute("ALTER TABLE settings DROP COLUMN dispatcher_heartbeat_timeout")


def test_migrate_settings_heartbeat_adds_column_and_defaults(tmp_path) -> None:
    source = tmp_path / "head.db"
    target = tmp_path / "cairn.db"
    _build_pre_heartbeat_database(source)

    report = migrate_settings_heartbeat(source, target)

    assert report.settings == 1
    db.configure(target)
    with db.connect(target) as conn:
        row = conn.execute(
            "SELECT intent_timeout, reason_timeout, dispatcher_heartbeat_timeout FROM settings WHERE rowid = 1"
        ).fetchone()
        assert row["intent_timeout"] == 15
        assert row["reason_timeout"] == 15
        assert row["dispatcher_heartbeat_timeout"] == 45


def test_migrate_settings_heartbeat_rejects_current_and_bad_targets(tmp_path) -> None:
    current = tmp_path / "current.db"
    with sqlite3.connect(current) as conn:
        conn.executescript(db.SCHEMA)
    with pytest.raises(ValueError, match="already has"):
        migrate_settings_heartbeat(current, tmp_path / "out.db")

    source = tmp_path / "head.db"
    _build_pre_heartbeat_database(source)
    with pytest.raises(ValueError):
        migrate_settings_heartbeat(source, source)
    existing = tmp_path / "out.db"
    existing.write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError):
        migrate_settings_heartbeat(source, existing)


def test_migrate_db_cli_exposes_version_subcommands(tmp_path) -> None:
    runner = CliRunner()
    legacy = tmp_path / "legacy.db"
    _build_legacy_database(legacy)

    result = runner.invoke(
        main,
        ["migrate-db", "multi-goal", "--db-path", str(legacy), "--target", str(tmp_path / "a.db")],
    )
    assert result.exit_code == 0

    head = tmp_path / "head.db"
    _build_multi_goal_head_database(head)
    result = runner.invoke(
        main,
        ["migrate-db", "intent-weight", "--db-path", str(head), "--target", str(tmp_path / "b.db")],
    )
    assert result.exit_code == 0
    with sqlite3.connect(tmp_path / "b.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(intents)")}
        assert "weight" in columns

    pre = tmp_path / "pre.db"
    _build_pre_heartbeat_database(pre)
    result = runner.invoke(
        main,
        ["migrate-db", "settings-heartbeat", "--db-path", str(pre), "--target", str(tmp_path / "c.db")],
    )
    assert result.exit_code == 0
    with sqlite3.connect(tmp_path / "c.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
        assert "dispatcher_heartbeat_timeout" in columns

    result = runner.invoke(main, ["migrate-db", "intent-weight", "--db-path", str(head), "--target", str(tmp_path / "b.db")])
    assert result.exit_code != 0
