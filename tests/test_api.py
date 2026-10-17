"""API suites: submission semantics, owner scoping, gate/cancel transitions.

All dependencies are faked (store / redis / arq pool) — no network, no DB.
"""
import asyncio
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api import main as api_main


class FakeStore:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.decisions: list[dict[str, Any]] = []

    def seed(self, task_id: str, user_id: str = "alice", status: str = "queued",
             current_gate: str | None = None) -> dict[str, Any]:
        row = {
            "id": task_id, "user_id": user_id, "title": "t", "prd": "PRD",
            "status": status, "current_gate": current_gate, "outcome": None,
            "error": None, "cancel_requested": False,
        }
        self.tasks[task_id] = row
        return row

    async def ping(self) -> None:
        return None

    async def create_task(self, user_id: str, title: str, prd: str) -> dict[str, Any]:
        row = {
            "id": str(uuid.uuid4()), "user_id": user_id, "title": title, "prd": prd,
            "status": "queued", "current_gate": None, "outcome": None,
            "error": None, "cancel_requested": False,
        }
        self.tasks[row["id"]] = row
        return row

    async def list_tasks(self, user_id: str, status: str | None = None,
                         limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = [t for t in self.tasks.values() if t["user_id"] == user_id
                and (status is None or t["status"] == status)]
        return rows[offset:offset + limit]

    async def get_task(self, task_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return row if row and row["user_id"] == user_id else None

    async def record_decision(self, task_id: str, gate: str, approved: bool,
                              feedback: str, drop_areas: list[str],
                              decided_by: str) -> str:
        decision_id = str(uuid.uuid4())
        self.decisions.append({
            "id": decision_id, "task_id": task_id, "gate": gate,
            "approved": approved, "feedback": feedback,
            "drop_areas": drop_areas, "decided_by": decided_by,
        })
        return decision_id

    async def cancel(self, task_id: str, current_status: str) -> str:
        if current_status == "running":
            self.tasks[task_id]["cancel_requested"] = True
            return "cancel_requested"
        if current_status in ("queued", "awaiting_gate"):
            self.tasks[task_id]["status"] = "cancelled"
            return "cancelled"
        return "conflict"


class FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def ping(self) -> None:
        return None

    async def publish(self, channel: str, data: str) -> None:
        self.published.append((channel, json.loads(data)))


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, list, str | None]] = []

    async def enqueue_job(self, name: str, *args, **kwargs) -> None:
        self.jobs.append((name, list(args), kwargs.get("_job_id")))


STORE, REDIS, QUEUE = FakeStore(), FakeRedis(), FakeQueue()


@pytest.fixture(autouse=True)
def _fresh_fakes():
    global STORE, REDIS, QUEUE
    STORE, REDIS, QUEUE = FakeStore(), FakeRedis(), FakeQueue()
    api_main.app.dependency_overrides[api_main.get_store] = lambda: STORE
    api_main.app.dependency_overrides[api_main.get_redis] = lambda: REDIS
    api_main.app.dependency_overrides[api_main.get_queue] = lambda: QUEUE
    yield
    api_main.app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(api_main.app)


ALICE = {"X-User-Id": "alice"}
BOB = {"X-User-Id": "bob"}


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "api"}


def test_submit_returns_202_and_enqueues_with_task_id(client):
    response = client.post("/v1/tasks", json={"title": "checkout", "prd": "PRD text"},
                           headers=ALICE)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    # queue-level idempotency: the job id IS the task id
    assert QUEUE.jobs == [("run_pipeline", [body["task_id"]], body["task_id"])]
    # state transitions ride the same channel the SSE endpoint relays
    assert REDIS.published[0][1]["type"] == "task.status"
    assert STORE.tasks[body["task_id"]]["status"] == "queued"


def test_submit_rejects_oversized_prd(client):
    response = client.post("/v1/tasks", json={"title": "t", "prd": "x" * (64 * 1024 + 1)},
                           headers=ALICE)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "prd_too_large"


def test_task_reads_are_owner_scoped(client):
    task_id = client.post("/v1/tasks", json={"title": "t", "prd": "P"},
                          headers=ALICE).json()["task_id"]
    # a miss for another user is a 404 — never a 403 (existence is not leaked)
    assert client.get(f"/v1/tasks/{task_id}", headers=BOB).status_code == 404
    assert client.get(f"/v1/tasks/{task_id}", headers=ALICE).status_code == 200
    listing = client.get("/v1/tasks", headers=BOB).json()["tasks"]
    assert listing == []


def test_gate_decision_requires_awaiting_task(client):
    task_id = client.post("/v1/tasks", json={"title": "t", "prd": "P"},
                          headers=ALICE).json()["task_id"]
    response = client.post(f"/v1/tasks/{task_id}/gates/design",
                           json={"approved": True}, headers=ALICE)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_transition"


def test_gate_decision_enqueues_resume_with_decision_id(client):
    task_id = str(uuid.uuid4())
    STORE.seed(task_id, status="awaiting_gate", current_gate="design")
    response = client.post(f"/v1/tasks/{task_id}/gates/design",
                           json={"approved": False, "feedback": "tighten",
                                 "drop_areas": ["interface specs"]},
                           headers=ALICE)
    assert response.status_code == 202
    assert len(STORE.decisions) == 1
    decision_id = response.json()["decision_id"]
    name, args, job_id = QUEUE.jobs[0]
    assert name == "resume_pipeline"
    assert args == [task_id, "design", {"approved": False, "feedback": "tighten",
                                        "drop_areas": ["interface specs"]}]
    # resume jobs are keyed by decision: replays collapse, new decisions differ
    assert job_id == f"{task_id}:resume:{decision_id}"


def test_gate_decision_rejects_mismatched_gate(client):
    task_id = str(uuid.uuid4())
    STORE.seed(task_id, status="awaiting_gate", current_gate="design")
    response = client.post(f"/v1/tasks/{task_id}/gates/cases",
                           json={"approved": True}, headers=ALICE)
    assert response.status_code == 409


def test_cancel_semantics_follow_state_machine(client):
    # queued: the API owns the cancelled transition — flip immediately
    queued = STORE.seed(str(uuid.uuid4()), status="queued")
    response = client.post(f"/v1/tasks/{queued['id']}/cancel", headers=ALICE)
    assert response.status_code == 202
    assert response.json()["result"] == "cancelled"
    assert queued["status"] == "cancelled"
    # terminal: conflict → 409
    done = STORE.seed(str(uuid.uuid4()), status="completed")
    assert client.post(f"/v1/tasks/{done['id']}/cancel", headers=ALICE).status_code == 409
    # running: cooperative flag only — the worker stops at the next boundary
    running = STORE.seed(str(uuid.uuid4()), status="running")
    response = client.post(f"/v1/tasks/{running['id']}/cancel", headers=ALICE)
    assert response.json()["result"] == "cancel_requested"
    assert running["cancel_requested"] is True and running["status"] == "running"
