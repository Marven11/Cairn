from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
import logging
import threading

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from cairn.dispatcher.config import DispatchConfig, validate_dispatcher_name, validate_prompt_resources
from cairn.server.auth import DISPATCHER_USERNAME
from cairn.server.models import DispatcherLoginResponse, ProjectDetail, ProjectSummary, Settings

LOG = logging.getLogger(__name__)


class ProtocolError(RuntimeError):
    def __init__(self, message: str, status_code: int, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class AuthError(ProtocolError):
    """The server rejected the dispatcher token (HTTP 401).

    Callers cancel all running work, drop every in-memory state and log in
    again to obtain a fresh token.
    """

    def __init__(self, message: str, response_text: str = ""):
        super().__init__(message, 401, response_text)


def raise_if_unauthorized(response: requests.Response, path: str) -> None:
    if response.status_code == 401:
        raise AuthError(f"dispatcher token rejected path={path}", response.text)


def login_dispatcher(
    base_url: str,
    name: str,
    hostname: str,
    *,
    api_key: str,
    timeout: float = 10.0,
    prior_dispatcher_id: str | None = None,
) -> DispatcherLoginResponse:
    """Exchange the dispatcher api key for a JWT via the runtime endpoint.

    ``prior_dispatcher_id`` continues a previous login of this process so its
    server-side identity (and project assignments) survives the re-login.
    """
    validate_dispatcher_name(name)
    response = requests.post(
        f"{base_url.rstrip('/')}/runtime/dispatchers/login",
        json={"name": name, "hostname": hostname, "dispatcher_id": prior_dispatcher_id},
        auth=(DISPATCHER_USERNAME, api_key),
        timeout=timeout,
    )
    if response.status_code == 401:
        raise AuthError("dispatcher login rejected: invalid credentials", response.text)
    if response.status_code == 422:
        detail = response.json()["detail"]
        if isinstance(detail, list):
            detail = "; ".join(
                f"{'.'.join(map(str, error['loc'])) if error['loc'] else 'body'}: {error['msg']}"
                for error in detail
            )
        raise ProtocolError(f"dispatcher login rejected (HTTP 422): {detail}", 422, response.text)
    response.raise_for_status()
    return DispatcherLoginResponse.model_validate(response.json())


@dataclass(slots=True)
class ApiResult:
    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass(slots=True)
class _EtagEntry[T]:
    etag: str
    data: T


class CairnClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        *,
        token: str,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._token = token
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()
        self._summary_etag_cache: dict[str, _EtagEntry[list[ProjectSummary]]] = {}
        self._detail_etag_cache: dict[str, _EtagEntry[ProjectDetail]] = {}
        self._etag_cache_lock = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list_projects(self) -> list[ProjectSummary]:
        return self._etag_get(self._summary_etag_cache, "/projects", self._summary_adapter.validate_json)

    def get_project(self, project_id: str) -> ProjectDetail:
        return self._etag_get(
            self._detail_etag_cache, f"/projects/{project_id}", ProjectDetail.model_validate_json
        )

    def _etag_get[T](self, cache: dict[str, _EtagEntry[T]], path: str, parse: Callable[[bytes], T]) -> T:
        with self._etag_cache_lock:
            entry = cache.get(path)
        headers = {"If-None-Match": entry.etag} if entry is not None else None
        response = self._session().get(self._url(path), headers=headers, timeout=self._timeout)
        if response.status_code == 304:
            if entry is None:
                raise ProtocolError(f"unexpected 304 without cached etag path={path}", 304)
            return entry.data
        raise_if_unauthorized(response, path)
        if response.status_code >= 400:
            with self._etag_cache_lock:
                cache.pop(path, None)
        response.raise_for_status()
        data = parse(response.content)
        etag = response.headers.get("ETag")
        if etag is not None:
            with self._etag_cache_lock:
                cache[path] = _EtagEntry(etag, data)
        return data

    def get_dispatch_config(self, name: str) -> DispatchConfig:
        validate_dispatcher_name(name)
        response = self._session().get(self._url(f"/dispatchers/{name}"), timeout=self._timeout)
        raise_if_unauthorized(response, f"/dispatchers/{name}")
        if response.status_code in (400, 404, 422):
            detail = response.json()["detail"]
            if isinstance(detail, list):
                detail = "; ".join(
                    f"{'.'.join(map(str, error['loc'])) if error['loc'] else 'config'}: {error['msg']}"
                    for error in detail
                )
            raise ProtocolError(f"dispatcher {name} (HTTP {response.status_code}): {detail}", response.status_code)
        response.raise_for_status()
        config = DispatchConfig.model_validate(response.json())
        validate_prompt_resources(config.runtime.prompt_group)
        return config

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        raise_if_unauthorized(response, "/settings")
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def export_project(self, project_id: str) -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": "yaml"},
            timeout=self._timeout,
        )
        raise_if_unauthorized(response, f"/projects/{project_id}/export")
        response.raise_for_status()
        return response.text

    def dispatcher_heartbeat(self) -> ApiResult:
        return self._request_json("POST", "/runtime/dispatchers/heartbeat", json={})

    def heartbeat(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json={"worker": worker},
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json={"worker": worker},
        )

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json={"worker": worker},
        )

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json={"worker": worker},
        )

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json={"worker": worker, "description": description},
        )

    def complete(
        self,
        project_id: str,
        from_ids: list[str],
        description: str,
        worker: str,
        goals: list[str],
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json={"from": from_ids, "description": description, "worker": worker, "goals": goals},
        )

    def create_intent(self, project_id: str, from_ids: list[str], description: str, creator: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents",
            json={"from": from_ids, "description": description, "creator": creator, "worker": None},
        )

    def boost_intent(self, project_id: str, intent_id: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/boost",
            json={},
        )

    def _request_json(self, method: str, path: str, json: dict[str, Any]) -> ApiResult:
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))
        raise_if_unauthorized(response, path)
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {self._token}"
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session
