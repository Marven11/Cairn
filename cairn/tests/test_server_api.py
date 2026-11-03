from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.app import create_app


@pytest.fixture
def client(tmp_path) -> TestClient:
    with TestClient(create_app(tmp_path)) as test_client:
        yield test_client


class MutableClock:
    def __init__(self, now: str) -> None:
        self.now = now

    def __call__(self) -> str:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> MutableClock:
    from cairn.server import services
    from cairn.server.routers import hints, intents, projects

    clock = MutableClock("2026-01-01T00:00:00Z")
    for module in (services, hints, intents, projects):
        monkeypatch.setattr(module, "utcnow", clock)
    return clock


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={
            "title": "test",
            "origin": "starting point",
            "goal": "finish",
            "hints": [{"content": "initial clue", "creator": "human"}],
        },
    )
    assert response.status_code == 201
    assert response.json()["project"]["bootstrap_enabled"] is True
    return response.json()["project"]["id"]


def test_project_workflow_create_conclude_complete_add_goal_reopen(client: TestClient) -> None:
    project_id = _create_project(client)

    detail = client.get(f"/projects/{project_id}").json()
    assert [fact["id"] for fact in detail["facts"]] == ["origin", "g001"]
    assert detail["project"]["status"] == "active"

    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    assert response.status_code == 201
    assert response.json()["id"] == "i001"
    assert response.json()["to"] == []

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "explorer"

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "explorer", "description": "new fact"},
    )
    assert response.status_code == 200
    assert response.json()["fact"]["id"] == "f001"
    assert response.json()["fact"]["created_at"]
    assert response.json()["intent"]["to"] == ["f001"]

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "goals": ["g001"], "description": "solved", "worker": "reasoner"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "completed"
    assert payload["fact"]["id"] == "f002"
    assert payload["intent"]["to"] == ["f002", "g001"]

    summary = [p for p in client.get("/projects").json() if p["id"] == project_id][0]
    assert summary["goal_count"] == 1
    assert summary["completed_goal_count"] == 1

    response = client.post(
        f"/projects/{project_id}/goals",
        json={"description": "also do this", "creator": "human"},
    )
    assert response.status_code == 201
    assert response.json()["project"]["status"] == "active"
    assert response.json()["fact"]["id"] == "g002"
    assert response.json()["fact"]["created_at"]

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["status"] == "active"
    assert [fact["id"] for fact in detail["facts"]] == ["origin", "g001", "f001", "f002", "g002"]

    # completed goals can anchor new intents
    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["g001", "g002"], "description": "pursue both goals", "creator": "reasoner", "worker": None},
    )
    assert response.status_code == 201
    assert response.json()["from"] == ["g001", "g002"]


def test_stopping_project_releases_claims_and_reason_but_keeps_hints_writable(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    response = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert response.status_code == 200
    assert response.json()["reason"] is None

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] is None
    assert client.post(
        f"/projects/{project_id}/hints",
        json={"content": "manual note", "creator": "human"},
    ).status_code == 201
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "blocked", "creator": "reasoner", "worker": None},
    ).status_code == 403


def test_intent_creation_accepts_goals_but_rejects_mismatched_worker(client: TestClient) -> None:
    project_id = _create_project(client)

    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["g001"], "description": "pursue the goal", "creator": "reasoner", "worker": None},
    ).status_code == 201
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "invalid", "creator": "reasoner", "worker": "explorer"},
    ).status_code == 400
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["f999"], "description": "missing fact", "creator": "reasoner", "worker": None},
    ).status_code == 404


def test_settings_and_export_are_backed_by_the_same_database(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.put("/settings", json={"intent_timeout": 30, "reason_timeout": 45})
    assert response.status_code == 200
    assert client.get("/settings").json() == {"intent_timeout": 30, "reason_timeout": 45}

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    assert "origin: starting point" in exported.text
    assert "id: g001" in exported.text
    assert "description: finish" in exported.text
    assert client.get(f"/projects/{project_id}/export?format=invalid").status_code == 400

    timeline = client.get(f"/projects/{project_id}/export?format=timeline")
    assert timeline.status_code == 200
    assert "GOAL DECLARED g001" in timeline.text


def test_expired_intent_and_reason_leases_can_be_reclaimed(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    )
    with db.connect(client.app.state.database_path) as conn:
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE project_id = ?",
            (project_id,),
        )
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (project_id,),
        )

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "worker-b"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "worker-b"

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )
    assert response.status_code == 200
    assert response.json()["reason"]["worker"] == "worker-b"


def test_live_reason_lease_rejects_competing_worker(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    ).status_code == 200

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    assert response.status_code == 409
    assert "worker-a" in response.json()["detail"]


def test_project_creation_persists_disabled_bootstrap_and_exports_it(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "no bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": False,
        },
    )

    assert response.status_code == 201
    project_id = response.json()["project"]["id"]
    assert client.get(f"/projects/{project_id}").json()["project"]["bootstrap_enabled"] is False
    assert "bootstrap_enabled: false" in client.get(f"/projects/{project_id}/export?format=yaml").text


def test_project_creation_rejects_invalid_bootstrap_enabled(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "invalid bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": "sometimes",
        },
    )

    assert response.status_code == 422


def test_list_projects_sorts_by_most_recent_activity(
    client: TestClient, clock: MutableClock
) -> None:
    clock.now = "2026-01-01T00:00:00Z"
    older = _create_project(client)
    clock.now = "2026-01-02T00:00:00Z"
    newer = _create_project(client)

    projects = client.get("/projects").json()
    assert [p["id"] for p in projects] == [newer, older]
    assert [p["last_activity_at"] for p in projects] == [
        "2026-01-02T00:00:00Z",
        "2026-01-01T00:00:00Z",
    ]

    clock.now = "2026-01-03T00:00:00Z"
    assert client.post(
        f"/projects/{older}/hints",
        json={"content": "later clue", "creator": "human"},
    ).status_code == 201
    projects = client.get("/projects").json()
    assert [p["id"] for p in projects] == [older, newer]
    assert projects[0]["last_activity_at"] == "2026-01-03T00:00:00Z"

    clock.now = "2026-01-04T00:00:00Z"
    assert client.post(
        f"/projects/{newer}/intents",
        json={
            "from": ["origin"],
            "description": "explore",
            "creator": "worker-a",
            "worker": "worker-a",
        },
    ).status_code == 201
    projects = client.get("/projects").json()
    assert [p["id"] for p in projects] == [newer, older]
    assert projects[0]["last_activity_at"] == "2026-01-04T00:00:00Z"

    clock.now = "2026-01-05T00:00:00Z"
    assert client.post(
        f"/projects/{newer}/intents/i001/conclude",
        json={"worker": "worker-a", "description": "found"},
    ).status_code == 200
    projects = client.get("/projects").json()
    assert [p["id"] for p in projects] == [newer, older]
    assert projects[0]["last_activity_at"] == "2026-01-05T00:00:00Z"


def test_list_projects_ties_break_on_newest_creation(
    client: TestClient, clock: MutableClock
) -> None:
    clock.now = "2026-01-01T00:00:00Z"
    first = _create_project(client)
    second = _create_project(client)

    projects = client.get("/projects").json()
    assert [p["id"] for p in projects] == [second, first]


def test_worker_heartbeats_do_not_count_as_project_activity(
    client: TestClient, clock: MutableClock
) -> None:
    clock.now = "2026-01-01T00:00:00Z"
    project_id = _create_project(client)
    clock.now = "2026-01-02T00:00:00Z"
    assert client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "explore",
            "creator": "worker-a",
            "worker": "worker-a",
        },
    ).status_code == 201
    clock.now = "2026-01-03T00:00:00Z"
    assert client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "worker-a"},
    ).status_code == 200
    clock.now = "2026-01-04T00:00:00Z"
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "new_facts"},
    ).status_code == 200

    projects = client.get("/projects").json()
    assert projects[0]["last_activity_at"] == "2026-01-02T00:00:00Z"

