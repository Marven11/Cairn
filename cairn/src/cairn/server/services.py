from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException

from cairn.server.models import Intent, ProjectMeta, ProjectReason, is_goal_id


@dataclass
class ProjectCompletion:
    completed_at: str
    completed_by_intent: str


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    return f"proj_{row['value']:03d}"


def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_intent_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "intent", "i", project_id)


def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def next_goal_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "goal", "g", project_id)


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "stopped", "completed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def get_goal_fact_rows(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM facts
        WHERE project_id = ? AND id GLOB 'g[0-9]*'
        ORDER BY created_at, rowid
        """,
        (project_id,),
    ).fetchall()


def get_completed_goal_ids(conn: sqlite3.Connection, project_id: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT t.fact_id AS fact_id
        FROM intent_targets t
        JOIN facts f ON f.id = t.fact_id AND f.project_id = t.project_id
        WHERE t.project_id = ? AND f.id GLOB 'g[0-9]*'
        """,
        (project_id,),
    ).fetchall()
    return {row["fact_id"] for row in rows}


def all_goals_completed(conn: sqlite3.Connection, project_id: str) -> bool:
    goal_rows = get_goal_fact_rows(conn, project_id)
    completed = get_completed_goal_ids(conn, project_id)
    return all(row["id"] in completed for row in goal_rows)


def complete_project_if_all_goals_done(conn: sqlite3.Connection, project_id: str) -> None:
    if not all_goals_completed(conn, project_id):
        return
    row = conn.execute("SELECT status FROM projects WHERE id = ?", (project_id,)).fetchone()
    assert row is not None
    if row["status"] == "completed":
        return
    clear_project_reason(conn, project_id)
    conn.execute(
        "UPDATE projects SET status = 'completed' WHERE id = ?",
        (project_id,),
    )


def reopen_project_if_completed(conn: sqlite3.Connection, project_id: str) -> None:
    row = conn.execute("SELECT status FROM projects WHERE id = ?", (project_id,)).fetchone()
    assert row is not None
    if row["status"] != "completed":
        return
    clear_project_reason(conn, project_id)
    conn.execute(
        "UPDATE projects SET status = 'active' WHERE id = ?",
        (project_id,),
    )


def derive_project_completion(
    conn: sqlite3.Connection, project_id: str
) -> ProjectCompletion | None:
    """Derive the project completion from the graph.

    Returns None unless every goal is covered by an intent's ``to``. The
    covering intent is always concluded: complete writes the fact, the
    conclusion and the goal edges in one transaction and rejects
    already-completed goals, so each goal has exactly one covering edge.
    The project completes when the last goal is covered; when several goals
    are covered in the same instant, the triggering intent is the edge
    written last.
    """
    goal_ids = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM facts WHERE project_id = ? AND id GLOB 'g[0-9]*'",
            (project_id,),
        ).fetchall()
    }
    if not goal_ids:
        return None
    edges: dict[str, tuple[str, int, str]] = {}
    for row in conn.execute(
        """
        SELECT it.fact_id, it.rowid AS edge_rowid, i.id AS intent_id, i.concluded_at
        FROM intent_targets it
        JOIN intents i ON i.id = it.intent_id AND i.project_id = it.project_id
        WHERE it.project_id = ? AND it.fact_id GLOB 'g[0-9]*'
        """,
        (project_id,),
    ).fetchall():
        edges[row["fact_id"]] = (row["concluded_at"], row["edge_rowid"], row["intent_id"])
    if set(edges) != goal_ids:
        return None
    trigger_at, _, trigger_intent = max(
        (moment, edge_rowid, intent_id) for moment, edge_rowid, intent_id in edges.values()
    )
    return ProjectCompletion(completed_at=trigger_at, completed_by_intent=trigger_intent)


def validate_goals_for_completion(
    conn: sqlite3.Connection,
    project_id: str,
    goal_ids: list[str],
    source_ids: Sequence[str],
) -> None:
    sources = set(source_ids)
    goal_rows = {row["id"]: row for row in get_goal_fact_rows(conn, project_id)}
    completed = get_completed_goal_ids(conn, project_id)
    for sid in sources:
        if is_goal_id(sid) and sid not in completed:
            raise HTTPException(
                409, f"Goal {sid} is not completed and cannot be used as completion evidence"
            )
    for gid in goal_ids:
        row = goal_rows.get(gid)
        if row is None:
            raise HTTPException(404, f"Goal {gid} not found")
        if gid in sources:
            raise HTTPException(409, f"Goal {gid} cannot be both a source and a completion of the same intent")
        if gid in completed:
            raise HTTPException(409, f"Goal {gid} is already completed")


def validate_intent_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Intent not found")
    return row


def get_claimable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["concluded_at"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_releasable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["concluded_at"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def intent_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    targets = conn.execute(
        "SELECT fact_id FROM intent_targets WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=[t["fact_id"] for t in targets],
        description=row["description"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
    )


def build_intents(conn: sqlite3.Connection, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [intent_to_model(conn, r, project_id) for r in rows]


def get_intent_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT intent_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["intent_timeout"]


def get_reason_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT reason_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["reason_timeout"]


def project_reason_from_row(row: sqlite3.Row) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def project_meta_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> ProjectMeta:
    completion = derive_project_completion(conn, row["id"])
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        bootstrap_enabled=bool(row["bootstrap_enabled"]),
        created_at=row["created_at"],
        completed_at=completion.completed_at if completion else None,
        completed_by_intent=completion.completed_by_intent if completion else None,
        reason=project_reason_from_row(row),
    )


def clear_project_reason(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_intent_timeout(conn)
    now = utcnow()
    query = """
        UPDATE intents
        SET worker = NULL
        WHERE worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
          AND concluded_at IS NULL
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def expire_reason_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_reason_timeout(conn)
    now = utcnow()
    query = """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE reason_worker IS NOT NULL
          AND reason_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(reason_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)
