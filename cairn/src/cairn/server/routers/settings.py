from fastapi import APIRouter, Depends, Request

from cairn.server.auth import require_user
from cairn.server.db import get_conn
from cairn.server.models import Settings, UpdateSettingsRequest

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=Settings)
def get_settings(request: Request):
    with get_conn(request) as conn:
        row = conn.execute(
            "SELECT intent_timeout, reason_timeout, dispatcher_heartbeat_timeout FROM settings WHERE rowid = 1"
        ).fetchone()
        return Settings(
            intent_timeout=row["intent_timeout"],
            reason_timeout=row["reason_timeout"],
            dispatcher_heartbeat_timeout=row["dispatcher_heartbeat_timeout"],
        )


@router.put("/settings", response_model=Settings, dependencies=[Depends(require_user)])
def update_settings(request: Request, body: UpdateSettingsRequest):
    with get_conn(request) as conn:
        row = conn.execute(
            "SELECT intent_timeout, reason_timeout, dispatcher_heartbeat_timeout FROM settings WHERE rowid = 1"
        ).fetchone()
        merged = {
            "intent_timeout": body.intent_timeout if body.intent_timeout is not None else row["intent_timeout"],
            "reason_timeout": body.reason_timeout if body.reason_timeout is not None else row["reason_timeout"],
            "dispatcher_heartbeat_timeout": (
                body.dispatcher_heartbeat_timeout
                if body.dispatcher_heartbeat_timeout is not None
                else row["dispatcher_heartbeat_timeout"]
            ),
        }
        conn.execute(
            "UPDATE settings SET intent_timeout = ?, reason_timeout = ?, dispatcher_heartbeat_timeout = ? WHERE rowid = 1",
            (merged["intent_timeout"], merged["reason_timeout"], merged["dispatcher_heartbeat_timeout"]),
        )
        return Settings.model_validate(merged)
