"""API suites: auth ingress, submission semantics, owner scoping, quota, gates.

All dependencies are faked (store / redis / arq pool) — no network, no DB.
Identity is a real bearer JWT (api/auth.py is pure crypto, no I/O to fake);
the seeded demo passwords are hashed once at import, not per test.
"""
import json
import os
import uuid
from typing import Any

os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0000")

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.auth import create_token, hash_password

DEMO_HASHES = {
    "alice": hash_password("alice-dev"),
    "bob": hash_password("bob-dev"),
    "carol": hash_password("carol-dev"),
}


class FakeStore:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.decisions: list[dict[str, Any]] = []
        self.artifacts: dict[str, list[dict[str, Any]]] = {}
        # mutable per-test: quota knobs for the reservation path
        self.quota: dict[str, int] = {"used": 0, "limit": 60}

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

    async def authenticate(self, username: str) -> dict[str, Any] | None:
        digest = DEMO_HASHES.get(username)
        if digest is None:
            return None
        tenant = "globex" if username == "carol" else "acme"
        return {"id": username, "tenant_id": tenant, "username": username,
                "pw_hash": digest}

    async def quota_status(self, tenant_id: str) -> dict[str, Any]:
        return dict(self.quota)

    async def list_artifacts(self, task_id: str, user_id: str) -> list[dict[str, Any]]:
        return list(self.artifacts.get(task_id, []))

    async def create_task(self, user_id: str, title: str, prd: str,
                          idempotency_key: str | None = None) -> tuple[dict[str, Any], bool]:
        if idempotency_key is not None:
            for row in self.tasks.values():
                if (row["user_id"] == user_id
                        and row.get("idempotency_key") == idempotency_key):
                    return row, True
        row = {
            "id": str(uuid.uuid4()), "user_id": user_id, "title": title, "prd": prd,
            "status": "queued", "current_gate": None, "outcome": None,
            "error": None, "cancel_requested": False,
            "idempotency_key": idempotency_key,
        }
        self.tasks[row["id"]] = row
        return row, False

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


ALICE = {"Authorization": f"Bearer {create_token('alice', 'acme')}"}
BOB = {"Authorization": f"Bearer {create_token('bob', 'acme')}"}
CAROL = {"Authorization": f"Bearer {create_token('carol', 'globex')}"}


def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "api"}


def test_v1_routes_reject_anonymous_and_broken_tokens(client):
    assert client.get("/v1/tasks").status_code == 401  # no header at all
    tampered = {"Authorization": "Bearer " + ALICE["Authorization"].removeprefix("Bearer ")[:-2] + "xx"}
    assert client.get("/v1/tasks", headers=tampered).status_code == 401
    assert client.get("/v1/tasks", headers={"Authorization": "Basic alice"}).status_code == 401


def test_login_exchanges_credentials_for_a_tenant_scoped_token(client):
    response = client.post("/v1/auth/token", json={"username": "alice", "password": "alice-dev"})
    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "acme"
    # the issued token really authenticates the route surface
    listing = client.get("/v1/tasks",
                         headers={"Authorization": f"Bearer {body['access_token']}"})
    assert listing.status_code == 200


def test_login_is_not_a_user_enumeration_oracle(client):
    # unknown user and wrong password answer with the same envelope
    r1 = client.post("/v1/auth/token", json={"username": "mallory", "password": "x"})
    r2 = client.post("/v1/auth/token", json={"username": "alice", "password": "wrong"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["error"]["code"] == r2.json()["error"]["code"] == "invalid_credentials"


def test_submit_reserves_the_structural_ceiling_against_tenant_quota(client):
    # acme used 58/60: a task worst-cased at 6 calls no longer fits
    STORE.quota = {"used": 58, "limit": 60}
    response = client.post("/v1/tasks", json={"title": "t", "prd": "P"}, headers=ALICE)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "quota_exceeded"
    assert response.headers["x-quota-used"] == "58"
    # globex has headroom — the same submission passes for carol
    STORE.quota = {"used": 58, "limit": 600}
    assert client.post("/v1/tasks", json={"title": "t", "prd": "P"},
                       headers=CAROL).status_code == 202


def test_submit_replays_the_original_task_for_a_seen_idempotency_key(client):
    headers = {**ALICE, "Idempotency-Key": "retry-of-request-1"}
    first = client.post("/v1/tasks", json={"title": "t", "prd": "P"}, headers=headers)
    replay = client.post("/v1/tasks", json={"title": "t", "prd": "P"}, headers=headers)
    assert first.status_code == 202 and replay.status_code == 202
    assert replay.json()["task_id"] == first.json()["task_id"]
    assert replay.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    # one task row, one queue job — the retry replays, it does not re-run
    assert len(STORE.tasks) == 1
    assert [j[0] for j in QUEUE.jobs] == ["run_pipeline"]


def test_submissions_without_a_key_are_never_replays(client):
    for _ in range(2):
        response = client.post("/v1/tasks", json={"title": "t", "prd": "P"}, headers=ALICE)
        assert response.status_code == 202
        assert "idempotent-replayed" not in response.headers
    assert len(STORE.tasks) == 2  # NULL keys never collide


def test_idempotency_keys_are_scoped_per_user(client):
    h = {"Idempotency-Key": "same-key-different-people"}
    t1 = client.post("/v1/tasks", json={"title": "t", "prd": "P"}, headers={**ALICE, **h})
    t2 = client.post("/v1/tasks", json={"title": "t", "prd": "P"}, headers={**BOB, **h})
    assert t1.json()["task_id"] != t2.json()["task_id"]
    assert "idempotent-replayed" not in t2.headers


def test_quota_headers_follow_openai_conventions(client):
    STORE.quota = {"used": 12, "limit": 60}
    ok = client.post("/v1/tasks", json={"title": "t", "prd": "P"}, headers=ALICE)
    assert ok.status_code == 202
    assert ok.headers["x-ratelimit-limit"] == "60"
    assert ok.headers["x-ratelimit-remaining"] == "42"  # 60 - 12 used - 6 reserved
    assert int(ok.headers["x-ratelimit-reset"]) > 0
    STORE.quota = {"used": 58, "limit": 60}
    blocked = client.post("/v1/tasks", json={"title": "t", "prd": "P"}, headers=ALICE)
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"].isdigit()  # delta seconds (RFC 9110)
    assert blocked.headers["x-ratelimit-remaining"] == "0"


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


def test_artifact_listing_is_owner_scoped(client):
    """The report cards render from this route — a stranger gets a 404, and
    even the owner sees only the artifacts of their own task."""
    task_id = client.post("/v1/tasks", json={"title": "t", "prd": "P"},
                          headers=ALICE).json()["task_id"]
    STORE.artifacts[task_id] = [
        {"id": "a1", "task_id": task_id, "kind": "design", "version": "v1",
         "idempotency_key": f"{task_id}:design:a1", "content": {"scope": ["checkout"]}},
    ]
    assert client.get(f"/v1/tasks/{task_id}/artifacts", headers=BOB).status_code == 404
    body = client.get(f"/v1/tasks/{task_id}/artifacts", headers=ALICE).json()
    assert [a["kind"] for a in body["artifacts"]] == ["design"]


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
