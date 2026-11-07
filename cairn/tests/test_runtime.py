from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from conftest import TEST_AUTH, basic_headers, bearer_headers, dispatcher_login
from fastapi.testclient import TestClient

from cairn.dispatcher.scheduler.loop import DispatcherLoop, _AuthSession
from cairn.server.app import create_app
from cairn.server.models import ProjectSummary
from cairn.server.runtime import DispatcherRegistry
from cairn.server.tokens import (
    generate_secret,
    issue_token,
    new_dispatcher_id,
    verify_token,
)

PROJECT_BODY = {"title": "t", "origin": "o", "goal": "g"}


@pytest.fixture
def client(tmp_path) -> TestClient:
    with TestClient(
        create_app(tmp_path, auth=TEST_AUTH),
        headers=basic_headers(TEST_AUTH.username, TEST_AUTH.password),
    ) as test_client:
        dispatcher_login(test_client)
        yield test_client


@pytest.fixture
def bare_client(tmp_path) -> TestClient:
    """A user client with no dispatcher logged in yet."""
    with TestClient(
        create_app(tmp_path, auth=TEST_AUTH),
        headers=basic_headers(TEST_AUTH.username, TEST_AUTH.password),
    ) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    response = client.post("/projects", json=PROJECT_BODY)
    assert response.status_code == 201, response.text
    return response.json()["project"]["id"]


def _age_heartbeats(app, seconds: float) -> None:
    """Simulate dispatcher heartbeats going stale without sleeping."""
    for record in app.state.dispatcher_registry._dispatchers.values():
        record.last_heartbeat_at -= seconds


# --------------------------------------------------------------------------- tokens


def _encode_b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _sign_with(secret: bytes, payload: dict) -> str:
    import base64
    import hashlib
    import hmac
    import json

    header = _encode_b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    encoded_payload = _encode_b64url(json.dumps(payload).encode())
    signature = base64.urlsafe_b64encode(
        hmac.new(secret, f"{header}.{encoded_payload}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{header}.{encoded_payload}.{signature}"


def test_tokens_round_trip_and_expiry() -> None:
    secret = generate_secret()
    dispatcher_id = new_dispatcher_id()
    token = issue_token(secret, dispatcher_id, 60)
    assert verify_token(secret, token) == dispatcher_id
    expired = issue_token(secret, dispatcher_id, 60, now=time.time() - 61)
    assert verify_token(secret, expired) is None
    assert verify_token(generate_secret(), token) is None
    header, _, signature = token.split(".")
    forged = f"{header}.eyJzdWIiOiJvdGhlciJ9.{signature}"
    assert verify_token(secret, forged) is None
    assert verify_token(secret, "not-a-jwt") is None


def test_verify_token_rejects_malformed_and_mistyped_payloads() -> None:
    import json

    secret = generate_secret()
    dispatcher_id = new_dispatcher_id()
    valid_payload = {
        "sub": dispatcher_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
    }

    # three segments but undecodable base64/json in one of them
    broken_header = "!!!." + _encode_b64url(json.dumps(valid_payload).encode()) + "." + _encode_b64url(b"sig")
    assert verify_token(secret, broken_header) is None
    undecodable_payload = _encode_b64url(b"{not json")
    parts = issue_token(secret, dispatcher_id, 60).split(".")
    garbled = f"{parts[0]}.{undecodable_payload}.{parts[2]}"
    assert verify_token(secret, garbled) is None

    # header alg replaced ("none"): signature must still bind the header
    import base64
    import hashlib
    import hmac

    none_header = _encode_b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    encoded_payload = _encode_b64url(json.dumps(valid_payload).encode())
    signing_input = f"{none_header}.{encoded_payload}".encode()
    valid_sig = base64.urlsafe_b64encode(hmac.new(secret, signing_input, hashlib.sha256).digest()).rstrip(b"=").decode()
    assert verify_token(secret, f"{none_header}.{encoded_payload}.{valid_sig}") is None

    assert verify_token(secret, _sign_with(secret, [1, 2, 3])) is None
    assert verify_token(secret, _sign_with(secret, {**valid_payload, "sub": 123})) is None
    assert verify_token(secret, _sign_with(secret, {**valid_payload, "exp": "soon"})) is None


# --------------------------------------------------------------------------- registry


def test_registry_keeps_offline_assignment_and_prunes_stale_entries() -> None:
    registry = DispatcherRegistry()
    first = registry.login("main", "host-a", heartbeat_timeout=45)
    assert registry.heartbeat(first.id) is True
    assert registry.heartbeat("missing") is False
    assert registry.ensure_assignment("proj_001") is first

    # an offline assigned dispatcher keeps its assignment (halted, not reassigned)
    first.last_heartbeat_at -= 100
    assert registry.ensure_assignment("proj_001") is first

    # a fresh login prunes long-offline unassigned records; records without
    # any heartbeat age from their session_started_at
    stale = registry.login("stale", "host-b", heartbeat_timeout=45)
    stale.session_started_at -= 45 * 10 + 1
    registry.login("fresh", "host-c", heartbeat_timeout=45)
    assert registry.get(stale.id) is None
    assert registry.get(first.id) is not None  # still assigned: kept


def test_prune_uses_each_records_frozen_timeout() -> None:
    registry = DispatcherRegistry()
    frozen = registry.login("main", "host-a", heartbeat_timeout=300)
    assert registry.heartbeat(frozen.id) is True
    frozen.last_heartbeat_at -= 60  # past a 5s setting's threshold, inside its own 300s window

    registry.login("next", "host-b", heartbeat_timeout=5)

    assert registry.get(frozen.id) is not None


def test_relogin_keeps_connected_at_and_refreshes_the_session_clock() -> None:
    registry = DispatcherRegistry()
    first = registry.login("main", "host-a", heartbeat_timeout=45)
    registry.heartbeat(first.id)
    registry.get(first.id).connected_at = time.time() - 1000

    relogged = registry.login("main", "host-a", heartbeat_timeout=45, prior_dispatcher_id=first.id)

    assert relogged.id == first.id
    assert relogged.last_heartbeat_at is None
    assert relogged.connected_at < time.time() - 999
    assert relogged.session_started_at > time.time() - 5

    registry.login("other", "host-b", heartbeat_timeout=45)  # another dispatcher's login triggers prune

    assert registry.get(first.id) is not None  # the renewed session is fresh, prune spares it
    assert registry.heartbeat(first.id) is True


# --------------------------------------------------------------------------- login / auth flow


def test_login_rejects_bad_names_and_reports_heartbeat_settings(client: TestClient) -> None:
    client.put("/settings", json={"intent_timeout": 15, "reason_timeout": 15, "dispatcher_heartbeat_timeout": 30})
    headers = basic_headers("dispatcher", TEST_AUTH.dispatcher_api_key)
    response = client.post("/runtime/dispatchers/login", json={"name": "../bad", "hostname": "h"}, headers=headers)
    assert response.status_code == 422
    response = client.post("/runtime/dispatchers/login", json={"name": "main", "hostname": ""}, headers=headers)
    assert response.status_code == 422
    # user basic credentials cannot log a dispatcher in
    response = client.post(
        "/runtime/dispatchers/login",
        json={"name": "main", "hostname": "h"},
        headers=basic_headers(TEST_AUTH.username, TEST_AUTH.password),
    )
    assert response.status_code == 401

    session = dispatcher_login(client)
    assert session["heartbeat_timeout"] == 30
    assert session["heartbeat_interval"] == 10
    assert len(session["dispatcher_id"]) == 21
    assert session["token"]


def test_dispatcher_heartbeat_requires_dispatcher_token(client: TestClient) -> None:
    session = dispatcher_login(client)
    # the fixture authenticates as the user: user credentials are rejected
    assert client.post("/runtime/dispatchers/heartbeat").status_code == 403
    assert (
        client.post(
            "/runtime/dispatchers/heartbeat",
            headers=basic_headers(TEST_AUTH.username, TEST_AUTH.password),
        ).status_code
        == 403
    )
    assert client.post("/runtime/dispatchers/heartbeat", headers=bearer_headers("junk")).status_code == 401
    assert client.post("/runtime/dispatchers/heartbeat", headers=bearer_headers(session["token"])).status_code == 200


def test_dispatcher_token_dies_with_the_server_session(client: TestClient) -> None:
    session = dispatcher_login(client)
    token = bearer_headers(session["token"])
    assert client.post("/runtime/dispatchers/heartbeat", headers=token).status_code == 200
    # forget the registry entry (as after a server restart): 401 -> re-login
    client.app.state.dispatcher_registry._dispatchers.pop(session["dispatcher_id"])
    assert client.post("/runtime/dispatchers/heartbeat", headers=token).status_code == 401


# --------------------------------------------------------------------------- projects <-> dispatchers


def test_project_creation_requires_an_online_dispatcher(tmp_path) -> None:
    with TestClient(create_app(tmp_path, auth=TEST_AUTH), headers=basic_headers(TEST_AUTH.username, TEST_AUTH.password)) as client:
        response = client.post("/projects", json=PROJECT_BODY)
        assert response.status_code == 503
        session = dispatcher_login(client)
        response = client.post("/projects", json=PROJECT_BODY)
        assert response.status_code == 201
        assert response.json()["project"]["dispatcher"]["id"] == session["dispatcher_id"]
        assert response.json()["project"]["status"] == "active"


def test_projects_are_assigned_to_the_earliest_online_dispatcher(bare_client: TestClient) -> None:
    first = dispatcher_login(bare_client, name="first", hostname="host-a")
    dispatcher_login(bare_client, name="second", hostname="host-b")

    project_id = _create_project(bare_client)
    summary = next(p for p in bare_client.get("/projects").json() if p["id"] == project_id)
    assert summary["dispatcher"]["id"] == first["dispatcher_id"]
    assert summary["dispatcher"]["online"] is True
    entries = bare_client.get("/runtime/dispatchers").json()
    assert [entry["name"] for entry in entries] == ["first", "second"]
    assert sum(entry["project_count"] for entry in entries) == 1


def test_assigned_dispatcher_offline_marks_project_halted(bare_client: TestClient) -> None:
    session = dispatcher_login(bare_client)
    project_id = _create_project(bare_client)
    assert bare_client.get(f"/projects/{project_id}").json()["project"]["status"] == "active"

    _age_heartbeats(bare_client.app, seconds=10_000)
    detail = bare_client.get(f"/projects/{project_id}").json()
    assert detail["project"]["status"] == "halted"
    assert detail["project"]["dispatcher"]["online"] is False
    summary = next(p for p in bare_client.get("/projects").json() if p["id"] == project_id)
    assert summary["status"] == "halted"
    assert summary["dispatcher"]["id"] == session["dispatcher_id"]

    # the dispatcher is still assigned: a heartbeat brings the project back
    bare_client.post("/runtime/dispatchers/heartbeat", headers=bearer_headers(session["token"]))
    summary = next(p for p in bare_client.get("/projects").json() if p["id"] == project_id)
    assert summary["status"] == "active"


def test_halted_projects_are_visible_via_etag_changes(client: TestClient) -> None:
    dispatcher_login(client)
    _create_project(client)
    etag_active = client.get("/projects").headers["etag"]
    _age_heartbeats(client.app, seconds=10_000)
    response = client.get("/projects", headers={"If-None-Match": etag_active})
    assert response.status_code == 200
    assert response.json()[0]["status"] == "halted"
    etag_halted = response.headers["etag"]
    assert etag_halted != etag_active
    assert client.get("/projects", headers={"If-None-Match": etag_halted}).status_code == 304


def test_user_can_switch_a_project_to_another_dispatcher(bare_client: TestClient) -> None:
    first = dispatcher_login(bare_client, name="first", hostname="host-a")
    second = dispatcher_login(bare_client, name="second", hostname="host-b")
    project_id = _create_project(bare_client)

    response = bare_client.put(
        f"/runtime/projects/{project_id}/dispatcher",
        json={"dispatcher_id": second["dispatcher_id"]},
    )
    assert response.status_code == 200
    assert response.json()["dispatcher"]["id"] == second["dispatcher_id"]

    summary = next(p for p in bare_client.get("/projects").json() if p["id"] == project_id)
    assert summary["dispatcher"]["id"] == second["dispatcher_id"]

    # the first dispatcher no longer sees the project as its own
    assigned_to_first = bare_client.get("/projects", headers=bearer_headers(first["token"])).json()
    assert all(p["dispatcher"]["id"] != first["dispatcher_id"] for p in assigned_to_first)

    # a malformed dispatcher id fails validation; a well-shaped unknown id
    # reports a missing dispatcher; an unknown project id reports missing
    assert (
        bare_client.put(f"/runtime/projects/{project_id}/dispatcher", json={"dispatcher_id": "nope"}).status_code
        == 422
    )
    assert (
        bare_client.put(
            f"/runtime/projects/{project_id}/dispatcher", json={"dispatcher_id": new_dispatcher_id()}
        ).status_code
        == 404
    )
    assert (
        bare_client.put(
            "/runtime/projects/proj_999/dispatcher", json={"dispatcher_id": second["dispatcher_id"]}
        ).status_code
        == 404
    )


def test_dispatcher_only_sees_dispatched_projects_in_list(bare_client: TestClient) -> None:
    first = dispatcher_login(bare_client, name="first", hostname="host-a")
    second = dispatcher_login(bare_client, name="second", hostname="host-b")
    project_id = _create_project(bare_client)  # assigned to the first dispatcher

    def loop_for(dispatcher_id: str) -> DispatcherLoop:
        summaries = [ProjectSummary.model_validate(p) for p in bare_client.get("/projects").json()]
        loop = DispatcherLoop.__new__(DispatcherLoop)
        loop._heartbeat_enabled = True
        loop._auth = _AuthSession(
            dispatcher_id=dispatcher_id,
            client=SimpleNamespace(list_projects=lambda: summaries),
            heartbeat_interval=first["heartbeat_interval"],
        )
        return loop

    assert [s.id for s in loop_for(first["dispatcher_id"])._list_assigned_projects()] == [project_id]

    reassignment = {"dispatcher_id": second["dispatcher_id"]}
    response = bare_client.put(f"/runtime/projects/{project_id}/dispatcher", json=reassignment)
    assert response.status_code == 200
    assert loop_for(first["dispatcher_id"])._list_assigned_projects() == []
    assert [s.id for s in loop_for(second["dispatcher_id"])._list_assigned_projects()] == [project_id]


def test_deleting_the_project_clears_the_assignment_for_counts(client: TestClient) -> None:
    dispatcher_login(client)
    project_id = _create_project(client)
    client.delete(f"/projects/{project_id}")
    registry = client.app.state.dispatcher_registry
    assert registry.assignment(project_id) is None


def test_stopped_and_completed_projects_keep_their_assignment_for_the_owner(client: TestClient) -> None:
    """Stopping or completing a project must not hide it from its assigned
    dispatcher: the dispatcher still owns the containers to clean up."""
    session = dispatcher_login(client)
    project_id = _create_project(client)

    client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    visible = client.get("/projects", headers=bearer_headers(session["token"])).json()
    entry = next(p for p in visible if p["id"] == project_id)
    assert entry["status"] == "stopped"
    assert entry["dispatcher"] is not None
    assert entry["dispatcher"]["id"] == session["dispatcher_id"]

    client.put(f"/projects/{project_id}/status", json={"status": "active"})
    client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["g001"], "description": "done", "worker": "w"},
    )
    visible = client.get("/projects", headers=bearer_headers(session["token"])).json()
    entry = next(p for p in visible if p["id"] == project_id)
    assert entry["status"] == "completed"
    assert entry["dispatcher"]["id"] == session["dispatcher_id"]


def test_project_payload_dispatcher_fields_stay_stable_across_heartbeats(client: TestClient) -> None:
    """Heartbeat timestamps must not leak into project payloads; otherwise
    every heartbeat invalidates the ETag of GET /projects."""
    session = dispatcher_login(client)
    project_id = _create_project(client)
    etag = client.get("/projects").headers["etag"]
    client.post("/runtime/dispatchers/heartbeat", headers=bearer_headers(session["token"]))
    response = client.get("/projects", headers={"If-None-Match": etag})
    assert response.status_code == 304
    info = next(p for p in client.get("/projects").json() if p["id"] == project_id)["dispatcher"]
    assert set(info) == {"id", "name", "hostname", "online"}


def test_heartbeat_timeout_change_applies_to_future_logins_only(bare_client: TestClient) -> None:
    session = dispatcher_login(bare_client)
    # the recorded timeout is snapshotted at login: shrinking the setting
    # must not open an offline window for this already-running dispatcher
    bare_client.put(
        "/settings",
        json={"intent_timeout": 15, "reason_timeout": 15, "dispatcher_heartbeat_timeout": 5},
    )
    _age_heartbeats(bare_client.app, seconds=10)
    summary = bare_client.get("/projects").json()  # no project yet
    assert summary == []
    info = bare_client.get("/runtime/dispatchers").json()[0]
    assert info["id"] == session["dispatcher_id"]
    assert info["online"] is True

    second = dispatcher_login(bare_client, name="second", hostname="host-b")
    assert second["heartbeat_timeout"] == 5


def test_relogin_with_prior_id_keeps_id_and_assignments(bare_client: TestClient) -> None:
    """JWT expiry forces a re-login; presenting the prior id keeps the
    dispatcher identity stable so its project assignments survive."""
    session = dispatcher_login(bare_client, name="main", hostname="host-a")
    project_id = _create_project(bare_client)  # auto-assigned

    again = bare_client.post(
        "/runtime/dispatchers/login",
        json={
            "name": "main",
            "hostname": "host-a",
            "dispatcher_id": session["dispatcher_id"],
        },
        headers=basic_headers("dispatcher", TEST_AUTH.dispatcher_api_key),
    ).json()
    assert again["dispatcher_id"] == session["dispatcher_id"]

    detail = bare_client.get(f"/projects/{project_id}").json()
    assert detail["project"]["status"] == "halted"

    beat = bare_client.post("/runtime/dispatchers/heartbeat", headers=bearer_headers(again["token"]))
    assert beat.status_code == 200

    info = bare_client.get("/runtime/dispatchers").json()[0]
    assert info["project_count"] == 1
    detail = bare_client.get(f"/projects/{project_id}").json()
    assert detail["project"]["status"] == "active"
    assert detail["project"]["dispatcher"]["id"] == session["dispatcher_id"]


def test_second_live_process_gets_its_own_id(bare_client: TestClient) -> None:
    """Logging in without a prior id must not take over the identity of
    another live dispatcher running the same config on the same machine."""
    first = dispatcher_login(bare_client, name="main", hostname="host-a")
    project_id = _create_project(bare_client)  # auto-assigned to the first

    login_headers = basic_headers("dispatcher", TEST_AUTH.dispatcher_api_key)
    second = bare_client.post(
        "/runtime/dispatchers/login",
        json={"name": "main", "hostname": "host-a"},
        headers=login_headers,
    ).json()
    assert second["dispatcher_id"] != first["dispatcher_id"]
    # the second process has not registered as beating yet
    listing = bare_client.get("/runtime/dispatchers").json()
    beaten = {entry["id"]: entry["last_heartbeat_at"] is not None for entry in listing}
    assert beaten[first["dispatcher_id"]] is True
    assert beaten[second["dispatcher_id"]] is False

    # while the beaten session stays alive, projects are claimable by it
    project_body = {"title": "t2", "origin": "o", "goal": "g"}
    assert bare_client.post("/projects", json=project_body).status_code == 201

    # once the beaten session is gone, an unbeaten login alone cannot claim
    # projects; after its first heartbeat it can
    first_record = bare_client.app.state.dispatcher_registry.get(first["dispatcher_id"])
    first_record.last_heartbeat_at = None
    assert bare_client.post("/projects", json={"title": "t3", "origin": "o", "goal": "g"}).status_code == 503
    second_beat = bare_client.post("/runtime/dispatchers/heartbeat", headers=bearer_headers(second["token"]))
    assert second_beat.status_code == 200
    assert bare_client.post("/projects", json={"title": "t3", "origin": "o", "goal": "g"}).status_code == 201

    counts = {entry["id"]: entry["project_count"] for entry in bare_client.get("/runtime/dispatchers").json()}
    assert counts[first["dispatcher_id"]] == 2  # proj_001 and t2 (created while it beat)
    assert counts[second["dispatcher_id"]] == 1
    detail = bare_client.get(f"/projects/{project_id}").json()
    assert detail["project"]["dispatcher"]["id"] == first["dispatcher_id"]


def test_prior_id_of_a_different_name_is_rejected_as_fresh(bare_client: TestClient) -> None:
    """A stale-id presentation only continues that identity when name and
    hostname match; anything else starts as a new instance."""
    first = dispatcher_login(bare_client, name="main", hostname="host-a")
    response = bare_client.post(
        "/runtime/dispatchers/login",
        json={
            "name": "other",
            "hostname": "host-a",
            "dispatcher_id": first["dispatcher_id"],
        },
        headers=basic_headers("dispatcher", TEST_AUTH.dispatcher_api_key),
    )
    assert response.status_code == 200
    assert response.json()["dispatcher_id"] != first["dispatcher_id"]


def test_login_request_field_validation(bare_client: TestClient) -> None:
    headers = basic_headers("dispatcher", TEST_AUTH.dispatcher_api_key)
    def _post(payload: dict):
        return bare_client.post("/runtime/dispatchers/login", json=payload, headers=headers)

    assert _post({"name": "bad/name", "hostname": "h"}).status_code == 422
    assert _post({"name": "x" * 251, "hostname": "h"}).status_code == 422
    assert _post({"name": "ok", "hostname": "???"}).status_code == 422
    assert _post({"name": "ok", "hostname": "h", "dispatcher_id": "short"}).status_code == 422
    assert _post({"name": "ok", "hostname": "h", "dispatcher_id": new_dispatcher_id()}).status_code == 200


def test_loop_relogin_after_auth_reset_carries_the_prior_id(monkeypatch) -> None:
    """A 401-driven auth reset drops the session, but the next login must
    still present the old dispatcher id so its assignments survive."""
    from cairn.dispatcher.protocol.client import CairnClient
    from cairn.dispatcher.scheduler.loop import DispatcherLoop, _AuthSession
    from cairn.server.models import DispatcherLoginResponse

    old_id = new_dispatcher_id()
    new_id = new_dispatcher_id()
    priors: list[str | None] = []

    def fake_login(server_url, name, hostname, *, api_key, prior_dispatcher_id=None):
        priors.append(prior_dispatcher_id)
        return DispatcherLoginResponse(
            dispatcher_id=new_id,
            token="t",
            heartbeat_interval=15,
            heartbeat_timeout=45,
        )

    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.server_url = "http://server"
    loop.name = "main"
    loop.hostname = "box"
    loop.api_key = "key"
    loop._heartbeat_enabled = True
    loop._relogin_prior_id = None
    loop.futures = {}
    loop.cleanup_futures = {}
    loop.reason_checkpoints = {}
    loop.runtime_project_ids = set()
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop._inactive_cleanup_done = {}
    loop._log_state = {}
    loop._auth = _AuthSession(
        dispatcher_id=old_id,
        client=CairnClient("http://server", token="expired"),
        heartbeat_interval=15,
    )
    monkeypatch.setattr("cairn.dispatcher.scheduler.loop.login_dispatcher", fake_login)

    loop._abandon_auth_session()
    assert loop._auth is None
    loop._login()

    assert priors == [old_id]
    assert loop._auth.dispatcher_id == new_id


def test_write_endpoints_report_the_runtime_status(client: TestClient) -> None:
    session = dispatcher_login(client)
    project_id = _create_project(client)
    _age_heartbeats(client.app, seconds=10_000)

    response = client.put(f"/projects/{project_id}/title", json={"title": "renamed"})
    meta = response.json()
    assert meta["status"] == "halted"
    assert meta["dispatcher"]["id"] == session["dispatcher_id"]

    client.post("/runtime/dispatchers/heartbeat", headers=bearer_headers(session["token"]))
    response = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    meta = response.json()
    assert meta["status"] == "stopped"
    assert meta["dispatcher"]["id"] == session["dispatcher_id"]


def test_switching_or_removing_assignments_updates_the_counted_totals(bare_client: TestClient) -> None:
    dispatcher_login(bare_client, name="first", hostname="host-a")
    second = dispatcher_login(bare_client, name="second", hostname="host-b")
    project_id = _create_project(bare_client)

    bare_client.put(
        f"/runtime/projects/{project_id}/dispatcher",
        json={"dispatcher_id": second["dispatcher_id"]},
    )
    counts = {entry["name"]: entry["project_count"] for entry in bare_client.get("/runtime/dispatchers").json()}
    assert counts == {"first": 0, "second": 1}

    bare_client.delete(f"/projects/{project_id}")
    counts = {entry["name"]: entry["project_count"] for entry in bare_client.get("/runtime/dispatchers").json()}
    assert counts == {"first": 0, "second": 0}


def test_server_restart_reassigns_to_the_first_dispatcher_back_online(tmp_path) -> None:
    user_headers = basic_headers(TEST_AUTH.username, TEST_AUTH.password)
    with TestClient(create_app(tmp_path, auth=TEST_AUTH), headers=user_headers) as first_boot:
        dispatcher_login(first_boot, name="main", hostname="host-a")
        project_id = _create_project(first_boot)

    # server restart: registry and jwt secret are gone; the project stays active
    # in the database but has no dispatcher until one logs in again
    with TestClient(create_app(tmp_path, auth=TEST_AUTH), headers=user_headers) as second_boot:
        summary = next(p for p in second_boot.get("/projects").json() if p["id"] == project_id)
        assert summary["status"] == "halted"
        assert summary["dispatcher"] is None

        second_session = dispatcher_login(second_boot, name="main", hostname="host-a")
        summary = next(p for p in second_boot.get("/projects").json() if p["id"] == project_id)
        assert summary["dispatcher"]["id"] == second_session["dispatcher_id"]
        assert summary["status"] == "active"
