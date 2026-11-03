"""One-shot migration tool: convert a legacy single-goal Cairn database to the
multi-goal schema (goals as ``gxxx`` facts, ``intent_targets`` junction table,
``facts.created_at``).

Legacy databases are not migrated in place by the server; run this tool once to
produce a converted database, then place it at ``<data-folder>/cairn.db``. Legacy
completion edges (``to_fact_id='goal'``) keep their intent, and a placeholder
fact is created so the ``to`` layout matches the protocol (first the conclusion
fact, then the completed goal).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cairn.server.db import SCHEMA


@dataclass
class MigrationReport:
    projects: int = 0
    goals_renamed: int = 0
    facts: int = 0
    placeholder_facts: int = 0
    intents: int = 0
    intent_targets: int = 0

    def summary(self) -> str:
        return "\n".join(
            [
                f"projects: {self.projects}",
                f"goal facts renamed to g-ids: {self.goals_renamed}",
                f"facts: {self.facts}",
                f"placeholder facts for legacy completion edges: {self.placeholder_facts}",
                f"intents: {self.intents}",
                f"intent targets: {self.intent_targets}",
            ]
        )


def migrate_database(source_path: Path, target_path: Path) -> MigrationReport:
    if source_path == target_path:
        raise ValueError("target path must differ from source path")
    if target_path.exists():
        raise ValueError(f"target path already exists: {target_path}")

    report = MigrationReport()
    source = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        source_columns = {
            row[1] for row in source.execute("PRAGMA table_info(facts)").fetchall()
        }
        if "created_at" in source_columns:
            raise ValueError(
                f"{source_path} already uses the multi-goal schema; nothing to migrate"
            )

        target = sqlite3.connect(str(target_path))
        target.row_factory = sqlite3.Row
        try:
            _run_migration(source, target, report)
            target.commit()
        finally:
            target.close()
    finally:
        source.close()

    return report


def _run_migration(
    source: sqlite3.Connection, target: sqlite3.Connection, report: MigrationReport
) -> None:
    target.executescript(SCHEMA)

    project_columns = {row[1] for row in source.execute("PRAGMA table_info(projects)").fetchall()}

    settings_row = source.execute("SELECT * FROM settings WHERE rowid = 1").fetchone()
    if settings_row is not None:
        target.execute(
            "UPDATE settings SET intent_timeout = ?, reason_timeout = ? WHERE rowid = 1",
            (settings_row["intent_timeout"], settings_row["reason_timeout"]),
        )

    projects = source.execute("SELECT * FROM projects ORDER BY created_at, rowid").fetchall()
    for project in projects:
        _migrate_project(source, target, project, project_columns, report)

    _sync_counters(target)


def _sync_counters(target: sqlite3.Connection) -> None:
    """Rebuild id counters from the rows actually written, so ids generated
    after the migration never collide with the migrated ids.
    """
    max_project = 0
    for (pid,) in target.execute("SELECT id FROM projects").fetchall():
        match = re.fullmatch(r"proj_(\d+)", pid)
        if match:
            max_project = max(max_project, int(match.group(1)))
    target.execute("UPDATE counters SET value = ? WHERE name = 'project'", (max_project,))

    scoped_queries = {
        "fact": ("facts", r"f(\d+)"),
        "intent": ("intents", r"i(\d+)"),
        "hint": ("hints", r"h(\d+)"),
        "goal": ("facts", r"g(\d+)"),
    }
    project_ids = [
        row["project_id"]
        for row in target.execute("SELECT DISTINCT project_id FROM facts").fetchall()
    ]
    for project_id in project_ids:
        for kind, (table, pattern) in scoped_queries.items():
            max_value = _max_scoped_id(target, project_id, table, pattern)
            target.execute(
                "INSERT INTO scoped_counters (project_id, kind, value) VALUES (?, ?, ?) "
                "ON CONFLICT(project_id, kind) DO UPDATE SET value = excluded.value",
                (project_id, kind, max_value),
            )


def _max_scoped_id(
    target: sqlite3.Connection, project_id: str, table: str, pattern: str
) -> int:
    prefix = pattern[0]
    max_value = 0
    for row in target.execute(
        f"SELECT id FROM {table} "
        f"WHERE project_id = ? AND id GLOB '{prefix}[0-9]*'",
        (project_id,),
    ).fetchall():
        match = re.fullmatch(pattern, row["id"])
        if match:
            max_value = max(max_value, int(match.group(1)))
    return max_value


def _migrate_project(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    project: sqlite3.Row,
    project_columns: set[str],
    report: MigrationReport,
) -> None:
    pid = project["id"]
    report.projects += 1
    target.execute(
        """
        INSERT INTO projects (id, title, status, bootstrap_enabled, created_at,
            reason_worker, reason_trigger, reason_started_at, reason_last_heartbeat_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pid,
            project["title"],
            project["status"],
            _bootstrap_enabled(project, project_columns),
            project["created_at"],
            project["reason_worker"],
            project["reason_trigger"],
            project["reason_started_at"],
            project["reason_last_heartbeat_at"],
        ),
    )

    facts = source.execute(
        "SELECT * FROM facts WHERE project_id = ? ORDER BY rowid",
        (pid,),
    ).fetchall()
    conclude_by_fact = {
        row["to_fact_id"]: row
        for row in source.execute(
            "SELECT to_fact_id, concluded_at, worker FROM intents WHERE project_id = ? AND to_fact_id IS NOT NULL",
            (pid,),
        ).fetchall()
    }

    fact_id_map: dict[str, str] = {}
    goal_count = 0
    for fact in facts:
        fid = fact["id"]
        new_id = fid
        is_legacy_goal = fid == "goal"
        if is_legacy_goal:
            goal_count += 1
            new_id = f"g{goal_count:03d}"
            report.goals_renamed += 1
        fact_id_map[fid] = new_id
        # the legacy goal fact is written at project creation time; ordinary
        # facts inherit the conclude time and worker of their producing intent
        if is_legacy_goal or fid not in conclude_by_fact:
            created_at = project["created_at"]
            creator = None
        else:
            created_at = conclude_by_fact[fid]["concluded_at"]
            creator = conclude_by_fact[fid]["worker"]
        target.execute(
            "INSERT INTO facts (id, project_id, description, created_at, creator) VALUES (?, ?, ?, ?, ?)",
            (new_id, pid, fact["description"], created_at, creator),
        )
        report.facts += 1

    for row in source.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at, rowid",
        (pid,),
    ).fetchall():
        target.execute(
            """
            INSERT INTO intents (id, project_id, description, creator, worker,
                last_heartbeat_at, created_at, concluded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                pid,
                row["description"],
                row["creator"],
                row["worker"],
                row["last_heartbeat_at"],
                row["created_at"],
                row["concluded_at"],
            ),
        )
        report.intents += 1

        for src in source.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (row["id"], pid),
        ).fetchall():
            mapped = fact_id_map.get(src["fact_id"], src["fact_id"])
            target.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (row["id"], pid, mapped),
            )

        if row["to_fact_id"] is not None:
            target_ids: list[str]
            if row["to_fact_id"] == "goal":
                # legacy completion edge: produce a placeholder fact so that the
                # to layout stays "conclusion fact first, completed goals after"
                placeholder_id = _next_placeholder_fact_id(target, pid)
                created_at = row["concluded_at"] or row["created_at"]
                target.execute(
                    "INSERT INTO facts (id, project_id, description, created_at, creator) VALUES (?, ?, ?, ?, ?)",
                    (
                        placeholder_id,
                        pid,
                        "placeholder fact for a legacy completion edge, created by migrate-db",
                        created_at,
                        row["worker"],
                    ),
                )
                report.facts += 1
                report.placeholder_facts += 1
                goal_target = fact_id_map["goal"]
                target_ids = [placeholder_id, goal_target]
            else:
                target_ids = [fact_id_map.get(row["to_fact_id"], row["to_fact_id"])]
            for target_id in target_ids:
                target.execute(
                    "INSERT INTO intent_targets (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                    (row["id"], pid, target_id),
                )
                report.intent_targets += 1

    for hint in source.execute(
        "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at, rowid",
        (pid,),
    ).fetchall():
        target.execute(
            "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            (hint["id"], pid, hint["content"], hint["creator"], hint["created_at"]),
        )


def _next_placeholder_fact_id(target: sqlite3.Connection, project_id: str) -> str:
    max_value = _max_scoped_id(target, project_id, "facts", r"f(\d+)")
    return f"f{max_value + 1:03d}"


def _bootstrap_enabled(row: sqlite3.Row, columns: set[str]) -> int:
    if "bootstrap_enabled" in columns and row["bootstrap_enabled"] is not None:
        return int(row["bootstrap_enabled"])
    if "bootstrap_mode" in columns and row["bootstrap_mode"] == "disabled":
        return 0
    return 1
