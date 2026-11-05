from fastapi import APIRouter, Depends, HTTPException, Request

from cairn.server.auth import require_user
from cairn.server.db import get_conn
from cairn.server.etag import etag
from cairn.server.models import (
    CompleteRequest,
    CompleteResponse,
    CreateGoalRequest,
    CreateProjectRequest,
    Fact,
    GoalResponse,
    Hint,
    HeartbeatRequest,
    ProjectDetail,
    ProjectMeta,
    ProjectSummary,
    ReasonClaimRequest,
    UpdateProjectTitleRequest,
    UpdateProjectStatusRequest,
)
from cairn.server.services import (
    build_intents,
    check_project_active,
    clear_project_reason,
    complete_project_if_all_goals_done,
    expire_reason_leases,
    expire_workers,
    get_project_or_404,
    intent_to_model,
    next_fact_id,
    next_goal_id,
    next_hint_id,
    next_intent_id,
    next_project_id,
    project_meta_from_row,
    reopen_project_if_completed,
    utcnow,
    validate_facts_exist,
    validate_goals_for_completion,
)

router = APIRouter(tags=["projects"])


@router.get("/projects", response_model=list[ProjectSummary])
@etag
def list_projects(request: Request):
    with get_conn(request) as conn:
        expire_workers(conn)
        expire_reason_leases(conn)
        # Last activity is the latest durable graph event; worker heartbeats are coordination, not activity.
        rows = conn.execute("""
            WITH project_activity AS (
                SELECT project_id, MAX(occurred_at) AS last_activity_at
                FROM (
                    SELECT id AS project_id, created_at AS occurred_at FROM projects
                    UNION ALL
                    SELECT project_id, created_at FROM hints
                    UNION ALL
                    SELECT project_id, created_at FROM intents
                    UNION ALL
                    SELECT project_id, concluded_at FROM intents WHERE concluded_at IS NOT NULL
                )
                GROUP BY project_id
            )
            SELECT p.*,
                activity.last_activity_at,
                (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NOT NULL) AS working_intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NULL) AS unclaimed_intent_count,
                (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count,
                (SELECT COUNT(*) FROM facts WHERE project_id = p.id AND id GLOB 'g[0-9]*') AS goal_count,
                (SELECT COUNT(DISTINCT t.fact_id) FROM intent_targets t
                    JOIN facts f ON f.id = t.fact_id AND f.project_id = t.project_id
                    WHERE t.project_id = p.id AND f.id GLOB 'g[0-9]*') AS completed_goal_count
            FROM projects p
            JOIN project_activity AS activity ON activity.project_id = p.id
            ORDER BY activity.last_activity_at DESC, p.rowid DESC
        """).fetchall()
        summaries = []
        for row in rows:
            meta = project_meta_from_row(conn, row)
            summaries.append(
                ProjectSummary(
                    **meta.model_dump(),
                    last_activity_at=row["last_activity_at"],
                    fact_count=row["fact_count"],
                    intent_count=row["intent_count"],
                    working_intent_count=row["working_intent_count"],
                    unclaimed_intent_count=row["unclaimed_intent_count"],
                    hint_count=row["hint_count"],
                    goal_count=row["goal_count"],
                    completed_goal_count=row["completed_goal_count"],
                )
            )
        return summaries


@router.post("/projects", response_model=ProjectDetail, status_code=201, dependencies=[Depends(require_user)])
def create_project(request: Request, body: CreateProjectRequest):
    with get_conn(request) as conn:
        pid = next_project_id(conn)
        now = utcnow()

        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) VALUES (?, ?, 'active', ?, ?)",
            (pid, body.title, body.bootstrap_enabled, now),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, created_at, creator) VALUES (?, ?, ?, ?, ?)",
            ("origin", pid, body.origin, now, None),
        )
        goal_id = next_goal_id(conn, pid)
        conn.execute(
            "INSERT INTO facts (id, project_id, description, created_at, creator) VALUES (?, ?, ?, ?, ?)",
            (goal_id, pid, body.goal, now, None),
        )

        hints = []
        if body.hints:
            for h in body.hints:
                hid = next_hint_id(conn, pid)
                conn.execute(
                    "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                    (hid, pid, h.content, h.creator, now),
                )
                hints.append(Hint(id=hid, content=h.content, creator=h.creator, created_at=now))

        return ProjectDetail(
            project=ProjectMeta(
                id=pid,
                title=body.title,
                status="active",
                bootstrap_enabled=body.bootstrap_enabled,
                created_at=now,
                reason=None,
            ),
            facts=[
                Fact(id="origin", description=body.origin, created_at=now, creator=None),
                Fact(id=goal_id, description=body.goal, created_at=now, creator=None),
            ],
            intents=[],
            hints=hints,
        )


@router.get("/projects/{project_id}", response_model=ProjectDetail)
@etag
def get_project(request: Request, project_id: str):
    with get_conn(request) as conn:
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)

        facts = conn.execute(
            """
            SELECT * FROM facts WHERE project_id = ?
            ORDER BY created_at, rowid
            """,
            (project_id,),
        ).fetchall()
        hints = conn.execute(
            "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()

        return ProjectDetail(
            project=project_meta_from_row(conn, row),
            facts=[Fact(**dict(f)) for f in facts],
            intents=build_intents(conn, project_id),
            hints=[Hint(**dict(h)) for h in hints],
        )


@router.delete("/projects/{project_id}", status_code=204, dependencies=[Depends(require_user)])
def delete_project(request: Request, project_id: str):
    with get_conn(request) as conn:
        get_project_or_404(conn, project_id)
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


@router.put("/projects/{project_id}/title", response_model=ProjectMeta, dependencies=[Depends(require_user)])
def update_project_title(request: Request, project_id: str, body: UpdateProjectTitleRequest):
    with get_conn(request) as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE projects SET title = ? WHERE id = ?",
            (body.title, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(conn, updated)


@router.put("/projects/{project_id}/status", response_model=ProjectMeta, dependencies=[Depends(require_user)])
def update_project_status(request: Request, project_id: str, body: UpdateProjectStatusRequest):
    with get_conn(request) as conn:
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_status = row["status"]
        if current_status == "completed":
            raise HTTPException(409, "Completed projects cannot change status")
        if current_status == body.status:
            return project_meta_from_row(conn, row)

        conn.execute(
            "UPDATE projects SET status = ? WHERE id = ?",
            (body.status, project_id),
        )
        if body.status == "stopped":
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE project_id = ? AND concluded_at IS NULL",
                (project_id,),
            )
            clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(conn, updated)


@router.post("/projects/{project_id}/reason/claim", response_model=ProjectMeta)
def claim_project_reason(request: Request, project_id: str, body: ReasonClaimRequest):
    with get_conn(request) as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is not None and current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
        if current_worker == body.worker:
            return project_meta_from_row(conn, row)

        now = utcnow()
        conn.execute(
            """
            UPDATE projects
            SET reason_worker = ?,
                reason_trigger = ?,
                reason_started_at = ?,
                reason_last_heartbeat_at = ?
            WHERE id = ?
            """,
            (body.worker, body.trigger, now, now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(conn, updated)


@router.post("/projects/{project_id}/reason/heartbeat", response_model=ProjectMeta)
def heartbeat_project_reason(request: Request, project_id: str, body: HeartbeatRequest):
    with get_conn(request) as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            raise HTTPException(409, "Project reason is not currently claimed")
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        now = utcnow()
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = ? WHERE id = ?",
            (now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(conn, updated)


@router.post("/projects/{project_id}/reason/release", response_model=ProjectMeta)
def release_project_reason(request: Request, project_id: str, body: HeartbeatRequest):
    with get_conn(request) as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            return project_meta_from_row(conn, row)
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(conn, updated)


@router.post("/projects/{project_id}/complete", response_model=CompleteResponse)
def complete_project(request: Request, project_id: str, body: CompleteRequest):
    with get_conn(request) as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)

        goal_ids = body.goals
        validate_goals_for_completion(conn, project_id, goal_ids, source_ids=body.from_)

        now = utcnow()
        iid = next_intent_id(conn, project_id)
        fid = next_fact_id(conn, project_id)

        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (iid, project_id, body.description, body.worker, body.worker, now, now, now),
        )
        for source_id in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, source_id),
            )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, created_at, creator) VALUES (?, ?, ?, ?, ?)",
            (fid, project_id, body.description, now, body.worker),
        )
        for target_id in [fid, *goal_ids]:
            conn.execute(
                "INSERT INTO intent_targets (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, target_id),
            )
        complete_project_if_all_goals_done(conn, project_id)

        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        updated_intent = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (iid, project_id),
        ).fetchone()
        assert updated_project is not None
        assert updated_intent is not None
        return CompleteResponse(
            project=project_meta_from_row(conn, updated_project),
            fact=Fact(id=fid, description=body.description, created_at=now, creator=body.worker),
            intent=intent_to_model(conn, updated_intent, project_id),
        )


@router.post("/projects/{project_id}/goals", response_model=GoalResponse, status_code=201, dependencies=[Depends(require_user)])
def create_goal(request: Request, project_id: str, body: CreateGoalRequest):
    with get_conn(request) as conn:
        get_project_or_404(conn, project_id)
        now = utcnow()
        gid = next_goal_id(conn, project_id)
        conn.execute(
            "INSERT INTO facts (id, project_id, description, created_at, creator) VALUES (?, ?, ?, ?, ?)",
            (gid, project_id, body.description, now, body.creator),
        )
        reopen_project_if_completed(conn, project_id)

        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        assert updated_project is not None
        return GoalResponse(
            project=project_meta_from_row(conn, updated_project),
            fact=Fact(id=gid, description=body.description, created_at=now, creator=body.creator),
        )
