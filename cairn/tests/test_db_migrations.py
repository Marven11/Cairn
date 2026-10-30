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
