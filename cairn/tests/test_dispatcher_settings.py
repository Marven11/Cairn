from __future__ import annotations

import json
import os

from click.testing import CliRunner
from fastapi import Request
from fastapi.testclient import TestClient
import pytest
import requests
import yaml

from cairn.cli import main
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db
from cairn.server.app import create_app

from conftest import make_config


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as http:
        yield http


@pytest.fixture
def source():
    return "# Keep this comment\n" + yaml.safe_dump(make_config().model_dump(mode="json"), sort_keys=False)


def test_first_start_initializes_missing_folder_and_reuses_persisted_data(tmp_path, source):
    data_folder = tmp_path / "new" / "cairn-data"
    assert not data_folder.exists()
    with TestClient(create_app(data_folder)) as first:
        assert first.get("/projects").json() == []
        assert first.get("/dispatchers").json() == []
        project = first.post("/projects", json={"title": "persisted", "origin": "start", "goal": "finish"})
        assert project.status_code == 201
        project_id = project.json()["project"]["id"]
        assert first.post("/dispatchers/main", json={"yaml": source}).status_code == 201
        assert first.put("/settings", json={"intent_timeout": 30, "reason_timeout": 45}).status_code == 200
    with TestClient(create_app(data_folder)) as second:
        assert second.get(f"/projects/{project_id}").json()["project"]["title"] == "persisted"
        assert second.get("/dispatchers/main/yaml").json() == {"yaml": source}
        assert second.get("/settings").json() == {"intent_timeout": 30, "reason_timeout": 45}


def test_dispatcher_crud_preserves_yaml_and_serves_model_dump(client, tmp_path, source):
    path = tmp_path / "dispatcher" / "Main_1-test.yaml"
    assert client.post("/dispatchers/Main_1-test", json={"yaml": source}).status_code == 201
    assert path.read_text() == source
    assert path.stat().st_mode & 0o777 == 0o600
    assert client.get("/dispatchers").json() == [{"name": "Main_1-test", "error": None}]
    assert client.get("/dispatchers/Main_1-test/yaml").json() == {"yaml": source}
    response = client.get("/dispatchers/Main_1-test")
    assert response.headers["content-type"] == "application/json"
    assert response.json() == make_config().model_dump(mode="json")
    assert "server" not in response.json()

    changed = source.replace("interval: 60", "interval: 2")
    assert client.put("/dispatchers/Main_1-test", json={"yaml": changed}).status_code == 200
    assert path.read_text() == changed
    assert client.get("/dispatchers/Main_1-test").json()["runtime"]["interval"] == 2
    assert client.delete("/dispatchers/Main_1-test").status_code == 204
    assert not path.exists()
    assert client.get("/dispatchers").json() == []


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_yaml_api_roundtrip_preserves_line_endings(client, tmp_path, source, newline):
    original = source.replace("\n", newline)
    path = tmp_path / "dispatcher" / "main.yaml"
    assert client.post("/dispatchers/main", json={"yaml": original}).status_code == 201
    assert path.read_bytes() == original.encode("utf-8")
    assert client.get("/dispatchers/main/yaml").json()["yaml"] == original
    assert client.get("/dispatchers/main").json() == make_config().model_dump(mode="json")
    updated = original + f"{newline}# Updated{newline}"
    assert client.put("/dispatchers/main", json={"yaml": updated}).status_code == 200
    assert path.read_bytes() == updated.encode("utf-8")
    assert client.get("/dispatchers/main/yaml").json()["yaml"] == updated


def test_duplicate_create_and_missing_updates_are_not_upserts(client, source):
    assert client.post("/dispatchers/main", json={"yaml": source}).status_code == 201
    assert client.post("/dispatchers/main", json={"yaml": source}).status_code == 409
    assert client.put("/dispatchers/missing", json={"yaml": source}).status_code == 404
    assert client.get("/dispatchers/missing").status_code == 404
    assert client.get("/dispatchers/missing/yaml").status_code == 404
    assert client.delete("/dispatchers/missing").status_code == 404


@pytest.mark.parametrize("invalid", ["", " ", "[", "null", "[]", "{}", "runtime: []", "!!python/object:builtins.str {}"])
def test_invalid_yaml_is_rejected_without_overwriting(client, source, invalid):
    client.post("/dispatchers/main", json={"yaml": source})
    assert client.put("/dispatchers/main", json={"yaml": invalid}).status_code == 422
    assert client.get("/dispatchers/main/yaml").json()["yaml"] == source
    assert client.post("/dispatchers/new", json={"yaml": invalid}).status_code == 422
    assert client.get("/dispatchers").json() == [{"name": "main", "error": None}]


def test_pydantic_validator_errors_are_json_serializable_and_omit_input(client):
    payload = make_config().model_dump(mode="json")
    payload["workers"].append(payload["workers"][0].copy())
    response = client.post("/dispatchers/main", json={"yaml": yaml.safe_dump(payload)})
    assert response.status_code == 422
    assert "worker names must be unique" in response.text
    assert all("input" not in error and "ctx" not in error for error in response.json()["detail"])


@pytest.mark.parametrize("name, expected_status", [
    ("main.yaml", 422), ("a%20b", 422), ("a%5Cb", 422),
    ("%E4%B8%AD", 422), ("name%0A", 422), ("%2e%2e%2foutside", 404),
])
def test_all_file_endpoints_reject_invalid_names(client, tmp_path, source, name, expected_status):
    outside = tmp_path / "outside.yaml"
    outside.write_text("outside")
    for method, suffix, body in [
        ("GET", "", None), ("GET", "/yaml", None),
        ("POST", "", {"yaml": source}), ("PUT", "", {"yaml": source}), ("DELETE", "", None),
    ]:
        response = client.request(method, f"/dispatchers/{name}{suffix}", json=body)
        assert response.status_code == expected_status
    assert outside.read_text() == "outside"
    assert client.get("/dispatchers").json() == []


@pytest.mark.parametrize("entry_type", ["symlink", "dangling_symlink", "directory", "fifo"])
def test_file_endpoints_reject_non_regular_files(client, tmp_path, source, entry_type):
    outside = tmp_path / "outside.yaml"
    outside.write_text(source)
    path = tmp_path / "dispatcher" / "main.yaml"
    if entry_type == "symlink":
        path.symlink_to(outside)
    elif entry_type == "dangling_symlink":
        path.symlink_to(tmp_path / "missing.yaml")
    elif entry_type == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    assert client.get("/dispatchers").json() == [
        {"name": "main", "error": "dispatcher config must be a regular file"}
    ]
    for method, suffix, body in [
        ("GET", "", None), ("GET", "/yaml", None),
        ("POST", "", {"yaml": source}), ("PUT", "", {"yaml": source}), ("DELETE", "", None),
    ]:
        assert client.request(method, f"/dispatchers/main{suffix}", json=body).status_code == 400
    assert outside.read_text() == source
    assert path.lstat()


def test_editor_can_repair_invalid_files_and_list_is_sorted(client, tmp_path, source):
    folder = tmp_path / "dispatcher"
    (folder / "empty.yaml").write_text("")
    (folder / "z.yaml").write_text("runtime: [")
    (folder / "a.yaml").write_text(source)
    (folder / "invalid.name.yaml").write_text(source)
    (folder / "unrelated.txt").write_text("ignore")
    entries = client.get("/dispatchers").json()
    assert [entry["name"] for entry in entries] == ["a", "empty", "invalid.name", "z"]
    assert [entry["name"] for entry in entries if entry["error"] is None] == ["a", "empty", "z"]
    assert "dispatcher name" in entries[2]["error"]
    assert client.get("/dispatchers/empty/yaml").json() == {"yaml": ""}
    assert client.get("/dispatchers/empty").status_code == 422
    assert client.put("/dispatchers/empty", json={"yaml": source}).status_code == 200
    assert client.get("/dispatchers/empty").status_code == 200
    assert client.get("/dispatchers/z").status_code == 422
    assert client.get("/dispatchers/z/yaml").json() == {"yaml": "runtime: ["}
    assert client.put("/dispatchers/z", json={"yaml": source}).status_code == 200
    assert client.get("/dispatchers/z").status_code == 200


def test_common_env_is_merged_in_json_but_not_rewritten_in_yaml(client):
    payload = make_config().model_dump(mode="json")
    payload["common_env"] = {"COMMON": "value", "OVERRIDE": "common"}
    payload["workers"][0]["env"] = {"OVERRIDE": "worker"}
    source = yaml.safe_dump(payload)
    client.post("/dispatchers/main", json={"yaml": source})
    assert client.get("/dispatchers/main").json()["workers"][0]["env"] == {"COMMON": "value", "OVERRIDE": "worker"}
    assert client.get("/dispatchers/main/yaml").json()["yaml"] == source


def test_dispatcher_fetches_config_then_validates_local_prompt_resources(client):
    payload = make_config().model_dump(mode="json")
    payload["runtime"]["prompt_group"] = "not-installed-here"
    assert client.post("/dispatchers/main", json={"yaml": yaml.safe_dump(payload)}).status_code == 201
    assert client.get("/dispatchers/main").status_code == 200
    api = CairnClient("http://testserver/")
    api._session = lambda: client
    with pytest.raises(ValueError, match="missing prompt group"):
        api.get_dispatch_config("main")


def test_dispatcher_loop_starts_with_remote_json_and_local_backend(client, monkeypatch):
    payload = make_config().model_dump(mode="json")
    payload["runtime"].update(execution="local", interval=1, prompt_group="mock")
    assert client.post("/dispatchers/main", json={"yaml": yaml.safe_dump(payload)}).status_code == 201
    monkeypatch.setattr(CairnClient, "_session", lambda _self: client)
    loop = DispatcherLoop("http://testserver/", "main")
    assert loop.config.runtime.execution == "local"
    loop.run(once=True)


def test_server_rejects_symlink_dispatcher_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "dispatcher").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        with TestClient(create_app(tmp_path)):
            pass


@pytest.mark.parametrize("value", ["", " ", "\t"])
def test_data_folder_argument_uses_standard_path_semantics(tmp_path, monkeypatch, value):
    observed = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cairn.cli.uvicorn.run", lambda app, **kwargs: observed.update(app=app))
    result = CliRunner().invoke(main, ["serve", "--data-folder", value])
    assert result.exit_code == 0
    assert observed["app"].state.database_path.parent == (tmp_path / value).resolve()


def test_dispatch_cli_passes_server_and_name_and_reports_fetch_failures(monkeypatch):
    observed = []

    class Loop:
        def __init__(self, server_url, name):
            observed.append((server_url, name))

        def run(self, *, once):
            observed.append(once)

    monkeypatch.setattr("cairn.cli.DispatcherLoop", Loop)
    result = CliRunner().invoke(main, ["dispatch", "--server-url", "http://server:9000", "--name", "Main_1", "--once"])
    assert result.exit_code == 0
    assert observed == [("http://server:9000", "Main_1"), True]
    result = CliRunner().invoke(main, ["dispatch", "--name", "../bad"])
    assert result.exit_code == 2

    def unavailable(*args):
        raise ValueError("missing prompt group: absent")

    monkeypatch.setattr("cairn.cli.DispatcherLoop", unavailable)
    result = CliRunner().invoke(main, ["dispatch", "--name", "main", "--once"])
    assert result.exit_code == 1
    assert "Error: missing prompt group" in result.output


def test_single_storage_path_drives_startup_database_and_dispatcher_files(tmp_path, source):
    unused = tmp_path / "unused"
    actual = tmp_path / "actual"
    app = create_app(unused)
    app.state.database_path = actual / "cairn.db"
    with TestClient(app) as client:
        response = client.post("/projects", json={"title": "actual", "origin": "start", "goal": "finish"})
        assert response.status_code == 201
        assert client.post("/dispatchers/main", json={"yaml": source}).status_code == 201
        assert client.get("/dispatchers/main/yaml").json()["yaml"] == source
        assert (actual / "dispatcher/main.yaml").read_text(encoding="utf-8") == source
        with db.connect(app.state.database_path) as conn:
            assert conn.execute("SELECT title FROM projects").fetchone()["title"] == "actual"
    assert not unused.exists()


def test_multiple_apps_isolate_database_and_yaml_in_their_data_folders(tmp_path, source):
    with TestClient(create_app(tmp_path / "first")) as first:
        with TestClient(create_app(tmp_path / "second")) as second:
            assert first.post("/projects", json={"title": "first", "origin": "start", "goal": "finish"}).status_code == 201
            assert second.post("/projects", json={"title": "second", "origin": "start", "goal": "finish"}).status_code == 201
            assert [p["title"] for p in first.get("/projects").json()] == ["first"]
            assert [p["title"] for p in second.get("/projects").json()] == ["second"]
            assert first.post("/dispatchers/main", json={"yaml": source}).status_code == 201
            assert second.get("/dispatchers").json() == []
            assert first.put("/settings", json={"intent_timeout": 30, "reason_timeout": 45}).status_code == 200
            assert second.get("/settings").json() == {"intent_timeout": 15, "reason_timeout": 15}
        assert first.get("/settings").json() == {"intent_timeout": 30, "reason_timeout": 45}
    assert (tmp_path / "first/cairn.db").is_file()
    assert (tmp_path / "second/cairn.db").is_file()


def test_dispatcher_names_respect_filesystem_component_limit(client, source):
    valid = "a" * 250
    assert client.post(f"/dispatchers/{valid}", json={"yaml": source}).status_code == 201
    assert client.get(f"/dispatchers/{valid}").status_code == 200
    for method, suffix, body in [
        ("GET", "", None), ("GET", "/yaml", None),
        ("POST", "", {"yaml": source}), ("PUT", "", {"yaml": source}), ("DELETE", "", None),
    ]:
        assert client.request(method, f"/dispatchers/{'a' * 251}{suffix}", json=body).status_code == 422
    assert client.get("/dispatchers").json() == [{"name": valid, "error": None}]


def test_request_and_explicit_database_connections_remain_separate(tmp_path):
    with TestClient(create_app(tmp_path / "first")) as first:
        first.post("/projects", json={"title": "first", "origin": "start", "goal": "finish"})
        with TestClient(create_app(tmp_path / "second")) as second:
            request = Request({"type": "http", "app": first.app})
            with db.get_conn(request) as conn:
                assert conn.execute("SELECT title FROM projects").fetchone()["title"] == "first"
            with db.connect(second.app.state.database_path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
            assert [project["title"] for project in first.get("/projects").json()] == ["first"]
            assert second.get("/projects").json() == []


def test_failed_database_initialization_is_not_reused(tmp_path, monkeypatch):
    path = tmp_path / "cairn.db"
    monkeypatch.setattr(db, "_reject_legacy_schema", lambda _conn: (_ for _ in ()).throw(RuntimeError("bad schema")))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="bad schema"):
            db.configure(path)


@pytest.mark.parametrize("failure", ["missing", "syntax", "schema", "model", "symlink", "directory", "fifo", "encoding"])
def test_dispatch_cli_reports_remote_config_errors_without_echoing_values(client, tmp_path, monkeypatch, failure):
    expected = "dispatcher config not found"
    if failure in ("symlink", "directory", "fifo", "encoding"):
        path = tmp_path / "dispatcher/broken.yaml"
        expected = "dispatcher config must be a regular file"
        if failure == "symlink":
            target = tmp_path / "outside.yaml"
            target.write_text("outside", encoding="utf-8")
            path.symlink_to(target)
        elif failure == "directory":
            path.mkdir()
        elif failure == "fifo":
            os.mkfifo(path)
        else:
            path.write_bytes(b"\xff")
            expected = "dispatcher config could not be read"
    elif failure == "syntax":
        (tmp_path / "dispatcher/broken.yaml").write_text("runtime: [")
        expected = "invalid YAML at line"
    elif failure in ("schema", "model"):
        payload = make_config().model_dump(mode="json")
        payload["common_env"] = {"API_KEY": "do-not-print-this-value"}
        if failure == "schema":
            payload["runtime"]["interval"] = 0
            expected = "runtime.interval: Input should be greater than 0"
        else:
            payload["workers"].append(payload["workers"][0].copy())
            expected = "config: Value error, worker names must be unique"
        (tmp_path / "dispatcher/broken.yaml").write_text(yaml.safe_dump(payload))
    monkeypatch.setattr(CairnClient, "_session", lambda _self: client)
    result = CliRunner().invoke(main, ["dispatch", "--name", "broken", "--server-url", "http://testserver", "--once"])
    assert result.exit_code == 1
    assert expected in result.output
    assert "do-not-print-this-value" not in result.output


@pytest.mark.parametrize("failure, expected", [
    ("field", "tasks: Field required"),
    ("model", "config: Value error, worker names must be unique"),
])
def test_dispatch_cli_identifies_fields_in_incompatible_server_responses(monkeypatch, failure, expected):
    payload = make_config().model_dump(mode="json")
    payload["common_env"] = {"API_KEY": "do-not-print-this-value"}
    if failure == "field":
        payload.pop("tasks")
    else:
        payload["workers"].append(payload["workers"][0].copy())
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(payload).encode("utf-8")
    session = requests.Session()
    monkeypatch.setattr(session, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(CairnClient, "_session", lambda _self: session)
    result = CliRunner().invoke(main, ["dispatch", "--name", "main", "--once"])
    assert result.exit_code == 1
    assert f"invalid dispatcher config: {expected}" in result.output
    assert "do-not-print-this-value" not in result.output
    session.close()


def test_removed_server_field_is_rejected(client, source):
    assert client.post("/dispatchers/main", json={"yaml": 'server: "http://old"\n' + source}).status_code == 422
