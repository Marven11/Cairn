from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from cairn.server.app import create_app


@pytest.fixture
def client(tmp_path) -> TestClient:
    with TestClient(create_app(tmp_path)) as test_client:
        yield test_client


def _create_project(client: TestClient, *, goal: str = "finish") -> str:
    response = client.post(
        "/projects",
        json={"title": "goals", "origin": "start", "goal": goal},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _declare_and_claim(client: TestClient, project_id: str, from_ids: list[str], description: str = "work") -> str:
    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": from_ids, "description": description, "creator": "reasoner", "worker": "reasoner"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_conclude_outputs_fact_only_and_keeps_project_active(client: TestClient) -> None:
    project_id = _create_project(client)
    intent_id = _declare_and_claim(client, project_id, ["origin"])

    response = client.post(
        f"/projects/{project_id}/intents/{intent_id}/conclude",
        json={"worker": "reasoner", "description": "flag captured"},
    )
    assert response.status_code == 200
    assert response.json()["intent"]["to"] == ["f001"]

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["status"] == "active"


def test_partial_goal_completion_keeps_project_active(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/goals",
        json={"description": "second objective", "creator": "human"},
    ).status_code == 201

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["g001"], "description": "first objective done", "worker": "reasoner"},
    )
    assert response.status_code == 200

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["status"] == "active"
    summary = [p for p in client.get("/projects").json() if p["id"] == project_id][0]
    assert summary["goal_count"] == 2
    assert summary["completed_goal_count"] == 1


def test_complete_with_explicit_goals_only_marks_listed_goals(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(f"/projects/{project_id}/goals", json={"description": "second objective", "creator": "human"})

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["g001"], "description": "first done", "worker": "reasoner"},
    )
    assert response.status_code == 200
    assert response.json()["project"]["status"] == "active"
    assert response.json()["intent"]["to"] == ["f001", "g001"]

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["status"] == "active"

    # completing the remaining goal finishes the project
    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "goals": ["g002"], "description": "second done", "worker": "reasoner"},
    )
    assert response.status_code == 200
    assert response.json()["project"]["status"] == "completed"


def test_complete_rejects_unknown_completed_and_empty_goals(client: TestClient) -> None:
    project_id = _create_project(client)

    assert client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["g999"], "description": "unknown", "worker": "reasoner"},
    ).status_code == 404
    assert client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["f001"], "description": "not a goal", "worker": "reasoner"},
    ).status_code == 404
    assert client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": [], "description": "empty", "worker": "reasoner"},
    ).status_code == 422

    # a goal completed by one intent cannot be completed again by another;
    # the project stays active because g002 is still open
    client.post(f"/projects/{project_id}/goals", json={"description": "second", "creator": "human"})
    assert client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["g001"], "description": "done", "worker": "reasoner"},
    ).status_code == 200
    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["g001"], "description": "again", "worker": "reasoner"},
    )
    assert response.status_code == 409
    assert "already completed" in response.json()["detail"]


# completing a goal may use other facts as sources, but never includes the same
# goal in both from and goals of one intent
def test_complete_rejects_goal_in_both_from_and_goals(client: TestClient) -> None:
    project_id = _create_project(client)
    # complete g001 through one intent so that g001 is an already-completed goal
    assert client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["g001"], "description": "flag found", "worker": "reasoner"},
    ).status_code == 200

    client.post(
        f"/projects/{project_id}/goals",
        json={"description": "second", "creator": "human"},
    )
    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["g001"], "goals": ["g001"], "description": "self", "worker": "reasoner"},
    )

    assert response.status_code == 409
    assert "cannot be both" in response.json()["detail"]


# completion evidence must be ordinary facts or already-completed goals;
# two open goals cannot serve as each other's evidence
def test_complete_rejects_open_goal_as_completion_evidence(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/goals",
        json={"description": "second", "creator": "human"},
    )
    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["g002"], "goals": ["g001"], "description": "g001 proven by g002", "worker": "r"},
    )

    assert response.status_code == 409
    assert "completion evidence" in response.json()["detail"]

    summary = [p for p in client.get("/projects").json() if p["id"] == project_id][0]
    assert summary["goal_count"] == 2
    assert summary["completed_goal_count"] == 0


def test_completed_goal_is_acceptable_completion_evidence(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["g001"], "description": "flag found", "worker": "reasoner"},
    ).status_code == 200

    client.post(
        f"/projects/{project_id}/goals",
        json={"description": "second", "creator": "human"},
    )
    intent_id = _declare_and_claim(client, project_id, ["f001", "g001"])
    assert client.post(
        f"/projects/{project_id}/intents/{intent_id}/conclude",
        json={"worker": "reasoner", "description": "second fact found"},
    ).status_code == 200
    assert client.post(
        f"/projects/{project_id}/complete",
        json={
            "from": ["f001", "g001"],
            "goals": ["g002"],
            "description": "second done",
            "worker": "reasoner",
        },
    ).status_code == 200

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["status"] == "completed"


def test_adding_goal_to_completed_project_reopens_and_keeps_history(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["g001"], "description": "done", "worker": "reasoner"},
    ).status_code == 200
    completed_meta = client.get(f"/projects/{project_id}").json()["project"]
    assert completed_meta["completed_at"] is not None

    response = client.post(
        f"/projects/{project_id}/goals",
        json={"description": "extend the work", "creator": "human"},
    )
    assert response.status_code == 201
    assert response.json()["project"]["status"] == "active"
    assert response.json()["fact"]["id"] == "g002"

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["status"] == "active"
    assert detail["project"]["completed_at"] is None
    # the previous completion intent and fact survive the reopen
    assert any(intent["to"] == ["f001", "g001"] for intent in detail["intents"])


def test_timeline_records_project_completion_per_completion_round(client: TestClient, monkeypatch) -> None:
    import cairn.server.routers.intents as intents_router
    import cairn.server.routers.projects as projects_router
    import cairn.server.services as services_module

    class Clock:
        def __init__(self) -> None:
            self.second = 0

        def __call__(self) -> str:
            self.second += 1
            return f"2026-01-01T00:00:{self.second:02d}.000Z"

    clock = Clock()
    monkeypatch.setattr(projects_router, "utcnow", clock)
    monkeypatch.setattr(intents_router, "utcnow", clock)
    monkeypatch.setattr(services_module, "utcnow", clock)

    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["g001"], "description": "done", "worker": "reasoner"},
    ).status_code == 200

    first_timeline = client.get(f"/projects/{project_id}/export?format=timeline").text
    assert first_timeline.count("PROJECT COMPLETED") == 1

    # a goal added in the same second as the completion must not hide the event while completed
    client.post(f"/projects/{project_id}/goals", json={"description": "more work", "creator": "human"})
    same_second_timeline = client.get(f"/projects/{project_id}/export?format=timeline").text
    assert "GOAL DECLARED g002" in same_second_timeline

    intent_response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "pursue g002", "creator": "reasoner", "worker": "reasoner"},
    )
    assert intent_response.status_code == 201
    assert client.post(
        f"/projects/{project_id}/intents/{intent_response.json()['id']}/conclude",
        json={"worker": "reasoner", "description": "g002 met"},
    ).status_code == 200
    assert client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["g002"], "description": "g002 met", "worker": "reasoner"},
    ).status_code == 200

    final_timeline = client.get(f"/projects/{project_id}/export?format=timeline").text
    assert final_timeline.count("GOAL COMPLETED g001") == 1
    assert final_timeline.count("GOAL COMPLETED g002") == 1
    assert final_timeline.count("PROJECT COMPLETED") == 1
    assert final_timeline.count("INTENT CONCLUDED i001") == 1
    assert final_timeline.count("INTENT CONCLUDED i002") == 1


def test_adding_goal_to_active_project_keeps_it_active(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"/projects/{project_id}/goals",
        json={"description": "another goal", "creator": "human"},
    )
    assert response.status_code == 201
    assert response.json()["project"]["status"] == "active"
    assert response.json()["fact"]["id"] == "g002"


def test_same_instant_dual_completion_marks_last_written_edge(client: TestClient, monkeypatch) -> None:
    import cairn.server.routers.intents as intents_router
    import cairn.server.routers.projects as projects_router
    import cairn.server.services as services_module

    class FrozenClock:
        def __call__(self) -> str:
            # every write lands on the same millisecond instant
            return "2026-01-01T00:00:01.000Z"

    clock = FrozenClock()
    monkeypatch.setattr(projects_router, "utcnow", clock)
    monkeypatch.setattr(intents_router, "utcnow", clock)
    monkeypatch.setattr(services_module, "utcnow", clock)

    project_id = _create_project(client)
    client.post(f"/projects/{project_id}/goals", json={"description": "second", "creator": "human"})

    intent_ids = {}
    for description in ("first", "second"):
        gid = "g001" if description == "first" else "g002"
        response = client.post(
            f"/projects/{project_id}/complete",
            json={"from": ["origin"], "goals": [gid], "description": description, "worker": "r"},
        )
        assert response.status_code == 200
        intent_ids[description] = response.json()["intent"]["id"]

    # both completions carry the same timestamp: the edge written last
    # (g002 by the second complete) is derived as the completion intent
    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["completed_by_intent"] == intent_ids["second"]

    timeline = client.get(f"/projects/{project_id}/export?format=timeline").text
    assert timeline.count("PROJECT COMPLETED") == 1
    completed_block = [line for line in timeline.splitlines() if line.startswith("[") and "PROJECT COMPLETED" in line][0]
    after = timeline.split(completed_block, 1)[1]
    assert f"via: {intent_ids['second']} from" in after


def test_intent_creation_deduplicates_from_ids(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin", "origin"], "description": "dup sources", "creator": "reasoner", "worker": None},
    )

    assert response.status_code == 201
    assert response.json()["from"] == ["origin"]


def test_timeline_export_includes_goal_events(client: TestClient, monkeypatch) -> None:
    import cairn.server.routers.intents as intents_router
    import cairn.server.routers.projects as projects_router
    import cairn.server.services as services_module

    class Clock:
        def __init__(self) -> None:
            self.second = 0

        def __call__(self) -> str:
            self.second += 1
            return f"2026-01-01T00:00:{self.second:02d}.000Z"

    clock = Clock()
    monkeypatch.setattr(projects_router, "utcnow", clock)
    monkeypatch.setattr(intents_router, "utcnow", clock)
    monkeypatch.setattr(services_module, "utcnow", clock)
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "goals": ["g001"], "description": "done", "worker": "reasoner"},
    ).status_code == 200

    completed_timeline = client.get(f"/projects/{project_id}/export?format=timeline").text
    assert "GOAL DECLARED g001" in completed_timeline
    assert "GOAL COMPLETED g001" in completed_timeline
    assert "PROJECT COMPLETED" in completed_timeline

    client.post(f"/projects/{project_id}/goals", json={"description": "more work", "creator": "human"})

    timeline = client.get(f"/projects/{project_id}/export?format=timeline").text
    assert "GOAL DECLARED g001" in timeline
    assert "GOAL COMPLETED g001" in timeline
    assert "GOAL DECLARED g002" in timeline
