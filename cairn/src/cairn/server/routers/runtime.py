from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from cairn.server.auth import (
    authenticate_dispatcher_login,
    require_dispatcher,
    require_user,
)
from cairn.server.db import get_conn
from cairn.server.models import (
    DispatcherLoginRequest,
    DispatcherLoginResponse,
    DispatcherSummary,
    ProjectDispatcherResponse,
    UpdateProjectDispatcherRequest,
)
from cairn.server.runtime import (
    DispatcherRegistry,
    dispatcher_info,
    dispatcher_summary,
)
from cairn.server.services import get_dispatcher_heartbeat_timeout, get_project_or_404
from cairn.server.tokens import issue_token

router = APIRouter(prefix="/runtime", tags=["runtime"])

DISPATCHER_TOKEN_TTL_SECONDS = 24 * 60 * 60


@router.post("/dispatchers/login", response_model=DispatcherLoginResponse)
def login_dispatcher(request: Request, body: DispatcherLoginRequest, _: None = Depends(authenticate_dispatcher_login)):
    with get_conn(request) as conn:
        heartbeat_timeout = get_dispatcher_heartbeat_timeout(conn)
    registry: DispatcherRegistry = request.app.state.dispatcher_registry
    record = registry.login(
        body.name,
        body.hostname,
        heartbeat_timeout,
        prior_dispatcher_id=body.dispatcher_id,
    )
    token = issue_token(request.app.state.dispatcher_jwt_secret, record.id, DISPATCHER_TOKEN_TTL_SECONDS)
    return DispatcherLoginResponse(
        dispatcher_id=record.id,
        token=token,
        heartbeat_interval=heartbeat_timeout // 3,
        heartbeat_timeout=heartbeat_timeout,
    )


@router.post("/dispatchers/heartbeat")
def dispatcher_heartbeat(
    request: Request, dispatcher_id: Annotated[str, Depends(require_dispatcher)]
) -> dict[str, str]:
    registry: DispatcherRegistry = request.app.state.dispatcher_registry
    if not registry.heartbeat(dispatcher_id):
        raise HTTPException(401, "dispatcher session expired; log in again")
    return {"status": "ok"}


@router.get("/dispatchers", response_model=list[DispatcherSummary], dependencies=[Depends(require_user)])
def list_dispatchers(request: Request):
    registry: DispatcherRegistry = request.app.state.dispatcher_registry
    return [dispatcher_summary(registry, record) for record in registry.snapshot()]


@router.put(
    "/projects/{project_id}/dispatcher",
    response_model=ProjectDispatcherResponse,
    dependencies=[Depends(require_user)],
)
def update_project_dispatcher(request: Request, project_id: str, body: UpdateProjectDispatcherRequest):
    with get_conn(request) as conn:
        get_project_or_404(conn, project_id)
    registry: DispatcherRegistry = request.app.state.dispatcher_registry
    record = registry.set_assignment(project_id, body.dispatcher_id)
    if record is None:
        raise HTTPException(404, "Dispatcher not found")
    return ProjectDispatcherResponse(
        project_id=project_id,
        dispatcher=dispatcher_info(record),
    )
