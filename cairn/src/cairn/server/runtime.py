"""In-memory registry of dispatcher instances and project assignments."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from cairn.server.models import DispatcherInfo, DispatcherSummary
from cairn.server.services import format_timestamp
from cairn.server.tokens import new_dispatcher_id

_PRUNE_TIMEOUT_MULTIPLIER = 10


@dataclass(slots=True)
class DispatcherRecord:
    id: str
    name: str
    hostname: str
    heartbeat_timeout: float
    connected_at: float
    session_started_at: float
    last_heartbeat_at: float | None = None


def is_record_online(record: DispatcherRecord) -> bool:
    return (
        record.last_heartbeat_at is not None
        and time.time() - record.last_heartbeat_at <= record.heartbeat_timeout
    )


def dispatcher_info(record: DispatcherRecord) -> DispatcherInfo:
    return DispatcherInfo(
        id=record.id,
        name=record.name,
        hostname=record.hostname,
        online=is_record_online(record),
    )


def dispatcher_summary(registry: DispatcherRegistry, record: DispatcherRecord) -> DispatcherSummary:
    return DispatcherSummary(
        id=record.id,
        name=record.name,
        hostname=record.hostname,
        online=is_record_online(record),
        connected_at=format_timestamp(record.connected_at),
        last_heartbeat_at=(
            format_timestamp(record.last_heartbeat_at) if record.last_heartbeat_at is not None else None
        ),
        project_count=registry.assignment_count(record.id),
    )


class DispatcherRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._dispatchers: dict[str, DispatcherRecord] = {}
        self._assignments: dict[str, str] = {}

    def login(
        self,
        name: str,
        hostname: str,
        heartbeat_timeout: float,
        *,
        prior_dispatcher_id: str | None = None,
    ) -> DispatcherRecord:
        """Continue the earlier session identified by ``prior_dispatcher_id``
        when name and hostname match, or mint a new id otherwise."""
        reused = self._continuable_session(name, hostname, prior_dispatcher_id)
        fresh = DispatcherRecord(
            id=reused.id if reused is not None else new_dispatcher_id(),
            name=name,
            hostname=hostname,
            heartbeat_timeout=heartbeat_timeout,
            connected_at=reused.connected_at if reused is not None else time.time(),
            session_started_at=time.time(),
            last_heartbeat_at=None,
        )
        with self._lock:
            self._prune_locked()
            self._dispatchers[fresh.id] = fresh
        return fresh

    def _continuable_session(
        self, name: str, hostname: str, dispatcher_id: str | None
    ) -> DispatcherRecord | None:
        if dispatcher_id is None:
            return None
        record = self._dispatchers.get(dispatcher_id)
        if record is not None and record.name == name and record.hostname == hostname:
            return record
        return None

    def get(self, dispatcher_id: str) -> DispatcherRecord | None:
        with self._lock:
            return self._dispatchers.get(dispatcher_id)

    def heartbeat(self, dispatcher_id: str) -> bool:
        with self._lock:
            record = self._dispatchers.get(dispatcher_id)
            if record is None:
                return False
            record.last_heartbeat_at = time.time()
            return True

    def snapshot(self) -> list[DispatcherRecord]:
        with self._lock:
            return sorted(self._dispatchers.values(), key=lambda record: (record.connected_at, record.id))

    def assignment(self, project_id: str) -> DispatcherRecord | None:
        with self._lock:
            dispatcher_id = self._assignments.get(project_id)
            if dispatcher_id is None:
                return None
            return self._dispatchers.get(dispatcher_id)

    def assignment_count(self, dispatcher_id: str) -> int:
        with self._lock:
            return sum(1 for assigned in self._assignments.values() if assigned == dispatcher_id)

    def _save_assignment(self, project_id: str, dispatcher_id: str) -> None:
        self._assignments[project_id] = dispatcher_id

    def ensure_assignment(self, project_id: str) -> DispatcherRecord | None:
        """Return the assigned dispatcher, picking the earliest online one
        when the project has no assignment (e.g. after a server restart)."""
        with self._lock:
            record = self.assignment(project_id)
            if record is not None:
                return record
            chosen = self._earliest_online_locked()
            if chosen is None:
                return None
            self._save_assignment(project_id, chosen.id)
            return chosen

    def set_assignment(self, project_id: str, dispatcher_id: str) -> DispatcherRecord | None:
        with self._lock:
            record = self._dispatchers.get(dispatcher_id)
            if record is None:
                return None
            self._save_assignment(project_id, dispatcher_id)
            return record

    def remove_project(self, project_id: str) -> None:
        with self._lock:
            self._assignments.pop(project_id, None)

    def _earliest_online_locked(self) -> DispatcherRecord | None:
        online = [record for record in self._dispatchers.values() if is_record_online(record)]
        if not online:
            return None
        return min(online, key=lambda record: (record.connected_at, record.id))

    def _prune_locked(self) -> None:
        counts: dict[str, int] = {}
        for dispatcher_id in self._assignments.values():
            counts[dispatcher_id] = counts.get(dispatcher_id, 0) + 1
        now = time.time()
        stale = [
            dispatcher_id
            for dispatcher_id, record in self._dispatchers.items()
            if now - (record.last_heartbeat_at or record.session_started_at)
            > record.heartbeat_timeout * _PRUNE_TIMEOUT_MULTIPLIER
            and counts.get(dispatcher_id, 0) == 0
        ]
        for dispatcher_id in stale:
            del self._dispatchers[dispatcher_id]
