from __future__ import annotations

from click.testing import CliRunner
from fastapi.testclient import TestClient
import pytest
import yaml

from cairn.cli import main
from cairn.server.app import create_app
from cairn.server.auth import DISPATCHER_USERNAME, _BASIC_REALM, AuthConfig

from conftest import TEST_AUTH, basic_headers, bearer_headers, dispatcher_login, make_config


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, auth=TEST_AUTH)


@pytest.fixture
def anonymous(app) -> TestClient:
    with TestClient(app) as client:
        yield client


@pytest.fixture
def user(app) -> TestClient:
    with TestClient(app, headers=basic_headers(TEST_AUTH.username, TEST_AUTH.password)) as client:
        yield client


@pytest.fixture
def dispatcher(app) -> TestClient:
    """A logged-in dispatcher instance issuing bearer-token requests."""
    with TestClient(app) as client:
        session = dispatcher_login(client)
        client.headers.update(bearer_headers(session["token"]))
        yield client


PROJECT_BODY = {"title": "t", "origin": "o", "goal": "g"}


def test_auth_config_rejects_unusable_credentials() -> None:
    with pytest.raises(ValueError, match="reserved name"):
        AuthConfig(username=DISPATCHER_USERNAME, password="p", dispatcher_api_key="k")
    with pytest.raises(ValueError, match="must not be the same value"):
        AuthConfig(username="u", password="same", dispatcher_api_key="same")
    with pytest.raises(ValueError, match="colon"):
        AuthConfig(username="a:b", password="pw", dispatcher_api_key="k")
    with pytest.raises(ValueError, match="ASCII"):
        AuthConfig(username="u", password="密码", dispatcher_api_key="k")
    with pytest.raises(ValueError, match="ASCII"):
        AuthConfig(username="u", password="p", dispatcher_api_key="café")


def test_healthz_docs_openapi_and_index_are_anonymous(anonymous: TestClient) -> None:
    assert anonymous.get("/healthz").status_code == 200
    assert anonymous.get("/healthz").json() == {"status": "ok"}
    assert anonymous.get("/openapi.json").status_code == 200
    assert anonymous.get("/docs").status_code == 200
    assert anonymous.get("/").status_code == 200
    # a project-owned static file is mounted and anonymously reachable
    assert anonymous.get("/static/favicon.svg").status_code == 200


def test_protected_endpoints_reject_missing_credentials(anonymous: TestClient) -> None:
    for path in ("/projects", "/settings", "/dispatchers"):
        response = anonymous.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == f'Basic realm="{_BASIC_REALM}"'
    response = anonymous.post("/projects", json=PROJECT_BODY)
    assert response.status_code == 401


def test_wrong_credentials_are_rejected(anonymous: TestClient) -> None:
    wrong = basic_headers("tester", "wrong")
    assert anonymous.get("/projects", headers=wrong).status_code == 401
    # the user password is not the dispatcher api key
    wrong_dispatcher = basic_headers(DISPATCHER_USERNAME, TEST_AUTH.password)
    assert anonymous.post("/runtime/dispatchers/login", json={"name": "main", "hostname": "h"}, headers=wrong_dispatcher).status_code == 401
    # an arbitrary username is not accepted either
    unknown = basic_headers("other", "secret")
    assert anonymous.get("/projects", headers=unknown).status_code == 401
    # a bogus bearer token is not accepted
    assert anonymous.get("/projects", headers=bearer_headers("not-a-jwt")).status_code == 401
    # basic dispatcher credentials only work on the login endpoint
    valid_dispatcher = basic_headers(DISPATCHER_USERNAME, TEST_AUTH.dispatcher_api_key)
    assert anonymous.get("/projects", headers=valid_dispatcher).status_code == 401


def test_user_can_call_every_endpoint(user: TestClient) -> None:
    dispatcher_login(user)
    # projects lifecycle
    assert user.post("/projects", json=PROJECT_BODY).status_code == 201
    project_id = user.get("/projects").json()[0]["id"]
    assert user.get(f"/projects/{project_id}").status_code == 200
    assert user.put(f"/projects/{project_id}/title", json={"title": "renamed"}).status_code == 200
    assert user.put(f"/projects/{project_id}/status", json={"status": "stopped"}).status_code == 200
    assert user.put(f"/projects/{project_id}/status", json={"status": "active"}).status_code == 200
    assert user.post(f"/projects/{project_id}/hints", json={"content": "clue", "creator": "human"}).status_code == 201
    assert user.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "explore", "creator": "reasoner", "worker": None},
    ).status_code == 201
    assert user.post(f"/projects/{project_id}/intents/i001/heartbeat", json={"worker": "worker-a"}).status_code == 200
    assert user.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "worker-a", "description": "found"},
    ).status_code == 200
    assert user.post(
        f"/projects/{project_id}/intents",
        json={"from": ["f001"], "description": "released work", "creator": "reasoner", "worker": None},
    ).status_code == 201
    assert user.post(f"/projects/{project_id}/intents/i002/heartbeat", json={"worker": "worker-b"}).status_code == 200
    assert user.post(f"/projects/{project_id}/intents/i002/release", json={"worker": "worker-b"}).status_code == 200
    assert user.post(
        f"/projects/{project_id}/reason/claim", json={"worker": "worker-a", "trigger": "bootstrap"}
    ).status_code == 200
    assert user.post(f"/projects/{project_id}/reason/heartbeat", json={"worker": "worker-a"}).status_code == 200
    assert user.post(f"/projects/{project_id}/reason/release", json={"worker": "worker-a"}).status_code == 200
    assert user.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "goals": ["g001"], "description": "solved", "worker": "worker-a"},
    ).status_code == 200
    assert user.post(f"/projects/{project_id}/goals", json={"description": "more", "creator": "human"}).status_code == 201
    assert user.get(f"/projects/{project_id}/export?format=yaml").status_code == 200

    # settings
    assert user.get("/settings").status_code == 200
    assert (
        user.put(
            "/settings",
            json={"intent_timeout": 30, "reason_timeout": 45, "dispatcher_heartbeat_timeout": 60},
        ).status_code
        == 200
    )

    # runtime dispatcher endpoints
    assert user.get("/runtime/dispatchers").status_code == 200

    # dispatcher configs CRUD
    assert user.get("/dispatchers").status_code == 200
    valid_yaml = yaml.safe_dump(make_config().model_dump(mode="json"))
    assert user.post("/dispatchers/main", json={"yaml": valid_yaml}).status_code == 201
    assert user.get("/dispatchers/main").status_code == 200
    assert user.get("/dispatchers/main/yaml").status_code == 200
    assert user.put("/dispatchers/main", json={"yaml": valid_yaml}).status_code == 200
    assert user.delete("/dispatchers/main").status_code == 204

    # user-only project removal
    assert user.delete(f"/projects/{project_id}").status_code == 204


def test_dispatcher_can_read_config_projects_settings_and_drive_graph(user: TestClient, dispatcher: TestClient) -> None:
    dispatcher_login(user)
    user.post("/projects", json=PROJECT_BODY)
    project_id = user.get("/projects").json()[0]["id"]

    assert dispatcher.get("/projects").status_code == 200
    assert dispatcher.get(f"/projects/{project_id}").status_code == 200
    assert dispatcher.get("/settings").status_code == 200
    assert dispatcher.get(f"/projects/{project_id}/export?format=yaml").status_code == 200
    assert dispatcher.post("/runtime/dispatchers/heartbeat").status_code == 200
    assert dispatcher.get("/dispatchers/main").status_code == 404  # config fetch itself is allowed
    assert (
        dispatcher.post(
            f"/projects/{project_id}/intents",
            json={"from": ["origin"], "description": "explore", "creator": "reasoner", "worker": None},
        ).status_code
        == 201
    )
    assert dispatcher.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "worker-a"},
    ).status_code == 200
    assert dispatcher.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    ).status_code == 200
    assert dispatcher.post(
        f"/projects/{project_id}/reason/heartbeat",
        json={"worker": "worker-a"},
    ).status_code == 200
    assert dispatcher.post(
        f"/projects/{project_id}/reason/release",
        json={"worker": "worker-a"},
    ).status_code == 200
    assert dispatcher.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "worker-a", "description": "done"},
    ).status_code == 200
    assert dispatcher.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "goals": ["g001"], "description": "solved", "worker": "worker-a"},
    ).status_code == 200


def test_dispatcher_cannot_call_user_only_endpoints(user: TestClient, dispatcher: TestClient, source: str) -> None:
    dispatcher_login(user)
    user.post("/projects", json=PROJECT_BODY)
    project_id = user.get("/projects").json()[0]["id"]

    assert dispatcher.post("/projects", json=PROJECT_BODY).status_code == 403
    assert dispatcher.delete(f"/projects/{project_id}").status_code == 403
    assert dispatcher.put(f"/projects/{project_id}/title", json={"title": "x"}).status_code == 403
    assert dispatcher.put(f"/projects/{project_id}/status", json={"status": "stopped"}).status_code == 403
    assert dispatcher.post(f"/projects/{project_id}/goals", json={"description": "g", "creator": "human"}).status_code == 403
    assert (
        dispatcher.post(
            f"/projects/{project_id}/hints",
            json={"content": "hint", "creator": "human"},
        ).status_code
        == 403
    )
    assert (
        dispatcher.put(
            "/settings",
            json={"intent_timeout": 30, "reason_timeout": 45, "dispatcher_heartbeat_timeout": 60},
        ).status_code
        == 403
    )
    assert dispatcher.get("/dispatchers").status_code == 403
    assert dispatcher.get("/dispatchers/main/yaml").status_code == 403
    assert dispatcher.get("/runtime/dispatchers").status_code == 403
    assert dispatcher.post("/dispatchers/other", json={"yaml": source}).status_code == 403
    assert dispatcher.put("/dispatchers/other", json={"yaml": source}).status_code == 403
    assert dispatcher.delete("/dispatchers/other").status_code == 403


def test_openapi_schema_stays_public_and_documents_auth(anonymous: TestClient) -> None:
    schema = anonymous.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/healthz" in paths
    assert "/projects" in paths
    assert "/dispatchers/{name}" in paths
    assert "/settings" in paths
    assert "/runtime/dispatchers/login" in paths
    assert paths["/projects"]["get"]["security"] == [{"HTTPBasic": []}, {"HTTPBearer": []}]
    assert paths["/runtime/dispatchers/login"]["post"]["security"] == [{"HTTPBasic": []}]
    assert "security" not in paths["/healthz"]["get"]


def test_serve_cli_requires_credentials(monkeypatch, tmp_path) -> None:
    for name in ("CAIRN_AUTH_USERNAME", "CAIRN_AUTH_PASSWORD", "CAIRN_DISPATCHER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("cairn.cli.uvicorn.run", lambda app, **kwargs: None)
    runner = CliRunner()

    result = runner.invoke(main, ["serve", "--data-folder", str(tmp_path)])
    assert result.exit_code == 2
    assert "--auth-username" in result.output

    result = runner.invoke(main, ["serve", "--data-folder", str(tmp_path), "--auth-username", "u"])
    assert result.exit_code == 2
    assert "--auth-password" in result.output

    result = runner.invoke(
        main, ["serve", "--data-folder", str(tmp_path), "--auth-username", "u", "--auth-password", "p"]
    )
    assert result.exit_code == 2
    assert "--dispatcher-api-key" in result.output


def test_serve_cli_rejects_reserved_username(tmp_path) -> None:
    result = CliRunner().invoke(
        main,
        [
            "serve",
            "--data-folder",
            str(tmp_path),
            "--auth-username",
            DISPATCHER_USERNAME,
            "--auth-password",
            "p",
            "--dispatcher-api-key",
            "k",
        ],
    )
    assert result.exit_code == 2
    assert "reserved name" in result.output


def test_serve_cli_reads_credentials_from_environment(monkeypatch, tmp_path) -> None:
    observed = {}
    monkeypatch.setattr("cairn.cli.uvicorn.run", lambda app, **kwargs: observed.update(app=app))
    monkeypatch.setenv("CAIRN_AUTH_USERNAME", "env-user")
    monkeypatch.setenv("CAIRN_AUTH_PASSWORD", "env-pass")
    monkeypatch.setenv("CAIRN_DISPATCHER_API_KEY", "env-key")
    result = CliRunner().invoke(main, ["serve", "--data-folder", str(tmp_path)])
    assert result.exit_code == 0
    with TestClient(observed["app"], headers=basic_headers("env-user", "env-pass")) as client:
        assert client.get("/projects").status_code == 200
    with TestClient(observed["app"], headers=basic_headers("env-user", "wrong")) as client:
        assert client.get("/projects").status_code == 401


def test_dispatch_cli_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("CAIRN_DISPATCHER_API_KEY", raising=False)
    result = CliRunner().invoke(main, ["dispatch", "--name", "main"])
    assert result.exit_code == 2
    assert "--api-key" in result.output


@pytest.fixture
def source() -> str:
    return yaml.safe_dump(make_config().model_dump(mode="json"), sort_keys=False)
