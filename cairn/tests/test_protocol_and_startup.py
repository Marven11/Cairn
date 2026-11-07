from __future__ import annotations

import json

import pytest
import requests

from cairn.dispatcher.protocol.client import CairnClient, ProtocolError
from cairn.dispatcher.runtime.startup_healthcheck import (
    StartupHealthcheckResult,
    format_failure_summary,
)


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requested_headers: list[dict[str, str] | None] = []

    def get(self, _url: str, headers=None, timeout=None) -> FakeResponse:
        self.requested_headers.append(headers)
        return self.responses.pop(0)


def test_client_request_failure_returns_status_zero() -> None:
    class Session:
        def request(self, *_args, **_kwargs):
            raise requests.ConnectionError("offline")

    client = CairnClient("http://server/", token="test-token")
    client._local.session = Session()

    result = client.create_intent("proj_001", ["f001"], "investigate", "reasoner")

    assert result.status_code == 0
    assert result.text == "offline"


def _project_detail_body(project_id: str) -> bytes:
    return json.dumps(
        {
            "project": {
                "id": project_id,
                "title": "test",
                "status": "active",
                "bootstrap_enabled": True,
                "created_at": "2026-01-01T00:00:00Z",
                "completed_at": None,
                "reason": None,
            },
            "facts": [],
            "intents": [],
            "hints": [],
        }
    ).encode()


def test_get_project_uses_etag_cache() -> None:
    body = _project_detail_body("proj_001")
    session = FakeSession(
        [
            FakeResponse(200, body, {"ETag": '"etag-v1"'}),
            FakeResponse(304, headers={"ETag": '"etag-v1"'}),
            FakeResponse(200, body, {"ETag": '"etag-v2"'}),
            FakeResponse(304, headers={"ETag": '"etag-v2"'}),
        ]
    )
    client = CairnClient("http://server/", token="test-token")
    client._local.session = session

    first = client.get_project("proj_001")
    assert first.project.id == "proj_001"
    assert session.requested_headers[0] is None

    second = client.get_project("proj_001")
    assert second.project.id == "proj_001"
    assert session.requested_headers[1] == {"If-None-Match": '"etag-v1"'}

    third = client.get_project("proj_001")
    assert third.project.id == "proj_001"
    assert session.requested_headers[2] == {"If-None-Match": '"etag-v1"'}

    fourth = client.get_project("proj_001")
    assert fourth.project.id == "proj_001"
    assert session.requested_headers[3] == {"If-None-Match": '"etag-v2"'}


def test_304_without_cache_entry_raises_protocol_error() -> None:
    session = FakeSession([FakeResponse(304)])
    client = CairnClient("http://server/", token="test-token")
    client._local.session = session

    with pytest.raises(ProtocolError) as exc_info:
        client.get_project("proj_001")
    assert exc_info.value.status_code == 304


def test_startup_healthcheck_failure_summary_includes_worker_details() -> None:
    results = [
        StartupHealthcheckResult(
            worker_name="worker-a",
            ok=False,
            status=401,
            duration_ms=12,
            detail="unauthorized",
            endpoint="POST http://api/v1/messages",
        ),
        StartupHealthcheckResult(
            worker_name="worker-b",
            ok=True,
            status=200,
            duration_ms=8,
            detail="",
            endpoint="POST http://api/v1/messages",
        ),
    ]

    summary = format_failure_summary(results)

    assert summary == (
        "startup healthchecks failed for all workers: worker-a(http=401, detail=unauthorized)"
    )
