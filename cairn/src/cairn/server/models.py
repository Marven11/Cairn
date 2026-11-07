from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from cairn.dispatcher.config import validate_dispatcher_name
from cairn.server.tokens import DISPATCHER_ID_PATTERN

GOAL_ID_PATTERN = re.compile(r"^g\d+$")

_HOSTNAME_RE = re.compile(r"[A-Za-z0-9._-]+")
_DISPATCHER_ID_RE = re.compile(DISPATCHER_ID_PATTERN)


def is_goal_id(fact_id: str) -> bool:
    return bool(GOAL_ID_PATTERN.match(fact_id))


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)
    dispatcher_heartbeat_timeout: int = Field(ge=5)


class UpdateSettingsRequest(BaseModel):
    intent_timeout: int | None = Field(default=None, ge=5)
    reason_timeout: int | None = Field(default=None, ge=5)
    dispatcher_heartbeat_timeout: int | None = Field(default=None, ge=5)


class Fact(BaseModel):
    id: str
    description: str
    created_at: str
    creator: str | None = None


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: list[str] = Field(default_factory=list)
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None
    weight: int = 0

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class DispatcherInfo(BaseModel):
    id: str
    name: str
    hostname: str
    online: bool


class DispatcherSummary(DispatcherInfo):
    connected_at: str
    last_heartbeat_at: str | None = None
    project_count: int


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "stopped", "completed", "halted"]
    bootstrap_enabled: bool
    created_at: str
    completed_at: str | None = None
    completed_by_intent: str | None = None
    reason: ProjectReason | None = None
    dispatcher: DispatcherInfo | None = None


class ProjectSummary(ProjectMeta):
    last_activity_at: str
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int
    goal_count: int
    completed_goal_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    bootstrap_enabled: bool = True
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateGoalRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class GoalResponse(BaseModel):
    project: ProjectMeta
    fact: Fact


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    # conclude no longer carries goal completions; an explicitly rejected extra
    # field keeps old protocol callers from silently getting a fake success
    model_config = {"extra": "forbid"}

    worker: str
    description: str

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str
    goals: list[str] = Field(min_length=1)

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned

    @field_validator("goals")
    @classmethod
    def validate_goal_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("goal ids must not be empty")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned


class CompleteResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class DispatcherLoginRequest(BaseModel):
    name: str
    hostname: str
    dispatcher_id: str | None = None

    @field_validator("name")
    @classmethod
    def validate_dispatcher_config_name(cls, value: str) -> str:
        text = value.strip()
        validate_dispatcher_name(text)
        return text

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, value: str) -> str:
        text = value.strip()
        if not _HOSTNAME_RE.fullmatch(text) or len(text) > 253:
            raise ValueError(
                "must be up to 253 letters, digits, dots, hyphens or underscores"
            )
        return text

    @field_validator("dispatcher_id")
    @classmethod
    def validate_dispatcher_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not _DISPATCHER_ID_RE.fullmatch(text):
            raise ValueError("must be a 21-character dispatcher id")
        return text


class DispatcherLoginResponse(BaseModel):
    dispatcher_id: str
    token: str
    heartbeat_interval: int
    heartbeat_timeout: int


class UpdateProjectDispatcherRequest(BaseModel):
    dispatcher_id: str

    @field_validator("dispatcher_id")
    @classmethod
    def validate_dispatcher_id(cls, value: str) -> str:
        text = value.strip()
        if not _DISPATCHER_ID_RE.fullmatch(text):
            raise ValueError("must be a 21-character dispatcher id")
        return text


class ProjectDispatcherResponse(BaseModel):
    project_id: str
    dispatcher: DispatcherInfo


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text
