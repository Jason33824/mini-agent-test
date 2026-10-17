"""Worker suites: the full task state machine, offline.

Real orchestrator + FakeLLM (same wiring as test_orchestrator); checkpointer is
an InMemorySaver, store/redis are fakes. arq itself is bypassed — the task
functions are invoked directly, so the suites exercise transition semantics.
"""
import asyncio
import json
import uuid
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agents.llm import FakeLLM
from agents.registry import AGENT_REGISTRY
from orchestrator import MAX_REVISE
from worker.main import resume_pipeline, run_pipeline

TID = "11111111-1111-1111-1111-111111111111"

DESIGN_DRAFT = {
    "version": "v1", "kind": "design", "task_id": TID,
    "idempotency_key": f"{TID}:design", "produced_by": "test_design",
    "scope": ["checkout"], "risks": ["double-charge"],
    "coverage_matrix": [{"area": "checkout", "risk": "double-charge", "covered_by": []}],
}
EVIDENCE = {
    "version": "v1", "kind": "evidence", "task_id": TID,
    "idempotency_key": f"{TID}:evidence", "produced_by": "whitebox",
    "items": [{"source": "interface specs", "finding": "negative amounts accepted",
               "risk_hint": "negative refund"}],
}
CASES = {
    "version": "v1", "kind": "cases", "task_id": TID,
    "idempotency_key": f"{TID}:cases", "produced_by": "test_case",
    "items": [{
        "title": "reject negative refund", "steps": ["submit amount=-1"],
        "data": "amount=-1", "assertion": "400 returned", "priority": "P0",
        "design_ref": "checkout",
    }],
}
REPORT = {
    "version": "v1", "kind": "report", "task_id": TID,
    "idempotency_key": f"{TID}:report", "produced_by": "test_execution",
    "results": [{"title": "reject negative refund", "passed": True, "detail": "400 as expected"}],
}


class FakeWorkerStore:
    """Faithful re-implementation of the conditional-transition semantics."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.artifacts: list[tuple[str, str]] = []
        self.transitions: list[tuple[str, str]] = []

    def seed(self, task_id: str, status: str = "queued",
             cancel_requested: bool = False) -> dict[str, Any]:
        row = {
            "id": task_id, "prd": "PRD text", "status": status,
            "current_gate": None, "outcome": None, "error": None,
            "cancel_requested": cancel_requested,
        }
        self.tasks[task_id] = row
        return row

    def _to(self, task_id: str, status: str) -> None:
        row = self.tasks[task_id]
        self.transitions.append((task_id, row["status"], status))
        row["status"] = status

    async def claim_runnable(self, task_id: str):
        row = self.tasks.get(task_id)
        if row and row["status"] in ("queued", "running"):
            self._to(task_id, "running")
            return {"id": task_id, "prd": row["prd"]}
        return None

    async def claim_resuming(self, task_id: str, gate: str):
        row = self.tasks.get(task_id)
        if row and row["status"] == "awaiting_gate" and row["current_gate"] == gate:
            self._to(task_id, "running")
            return {"id": task_id, "prd": row["prd"]}
        return None

    async def set_awaiting(self, task_id: str, gate: str) -> None:
        self.tasks[task_id]["current_gate"] = gate
        self._to(task_id, "awaiting_gate")

    async def cancel_requested(self, task_id: str) -> bool:
        return self.tasks[task_id]["cancel_requested"]

    async def mark_cancelled(self, task_id: str) -> None:
        self._to(task_id, "cancelled")

    async def complete(self, task_id: str, outcome: str) -> None:
        self.tasks[task_id]["outcome"] = outcome
        self.transitions.append((task_id, "running", "completed"))
        self.tasks[task_id]["status"] = "completed"

    async def fail(self, task_id: str, error: str) -> None:
        self.tasks[task_id]["error"] = error
        self._to(task_id, "failed")

    async def save_artifacts(self, task_id: str, values: dict[str, Any]):
        saved = []
        for kind, field in (("design", "design"), ("evidence", "evidence"),
                            ("cases", "cases"), ("report", "report")):
            if values.get(field):
                saved.append((kind, f"{task_id}:{kind}"))
        self.artifacts.extend((task_id, kind) for kind, _ in saved)
        return saved


class FakeRedis:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def publish(self, channel: str, data: str) -> None:
        self.events.append((channel, json.loads(data)))

    def types(self) -> list[str]:
        return [event["type"] for _, event in self.events]


def _wire(design_scripted: int = 1) -> None:
    AGENT_REGISTRY["test_design"].llm = FakeLLM(
        {"test-design agent": [dict(DESIGN_DRAFT) for _ in range(design_scripted)]}
    )
    AGENT_REGISTRY["whitebox"].llm = FakeLLM({"whitebox analysis agent": [dict(EVIDENCE)]})
    AGENT_REGISTRY["test_case"].llm = FakeLLM({"test-case agent": [dict(CASES)]})
    AGENT_REGISTRY["test_execution"].llm = FakeLLM({"test-execution agent": [dict(REPORT)]})


@pytest.fixture(autouse=True)
def _reset_llm_injection():
    yield
    for agent in AGENT_REGISTRY.values():
        agent.llm = None


def _ctx(store: FakeWorkerStore, redis: FakeRedis,
         checkpointer: InMemorySaver | None = None) -> dict:
    """One checkpointer per task under test: production runs a process-wide
    PostgresSaver, so run + resume of the same task must share one backend —
    a fresh InMemorySaver per call would lose the gate checkpoint entirely."""
    return {"store": store, "redis": redis,
            "checkpointer": checkpointer or InMemorySaver(), "job_try": 1}


def _gate_requests(redis: FakeRedis) -> list[dict]:
    return [event["payload"] for _, event in redis.events if event["type"] == "gate.request"]


def test_full_collaboration_reaches_report():
    _wire()
    store, redis = FakeWorkerStore(), FakeRedis()
    checkpointer = InMemorySaver()  # shared across run + resumes, like prod
    store.seed(TID)

    assert asyncio.run(run_pipeline(_ctx(store, redis, checkpointer), TID)) == "awaiting_gate"
    row = store.tasks[TID]
    assert row["status"] == "awaiting_gate" and row["current_gate"] == "design"
    # gate.request carries the artifact a human needs to review (LLD §8)
    first_gate = _gate_requests(redis)[0]
    assert first_gate["stage"] == "design" and first_gate["artifact"]["kind"] == "design"

    assert asyncio.run(
        resume_pipeline(_ctx(store, redis, checkpointer), TID, "design",
                        {"approved": True, "feedback": ""})
    ) == "awaiting_gate"  # now suspended on the second gate
    assert store.tasks[TID]["current_gate"] == "cases"

    assert asyncio.run(
        resume_pipeline(_ctx(store, redis, checkpointer), TID, "cases",
                        {"approved": True, "feedback": ""})
    ) == "report"
    row = store.tasks[TID]
    assert row["status"] == "completed" and row["outcome"] == "report"
    assert {kind for _, kind in store.artifacts} == {"design", "evidence", "cases", "report"}
    assert redis.types().count("gate.request") == 2
    assert "task.completed" in redis.types()
    # single-writer sanity: no jump transitions in the ledger
    legal = {("queued", "running"), ("running", "awaiting_gate"),
             ("awaiting_gate", "running"), ("running", "completed")}
    assert all((f, t) in legal for _, f, t in store.transitions)


def test_gate_rejection_runs_bounded_revise_then_surfaces():
    _wire(design_scripted=MAX_REVISE)
    store, redis = FakeWorkerStore(), FakeRedis()
    checkpointer = InMemorySaver()  # shared across run + resumes, like prod
    store.seed(TID)

    assert asyncio.run(run_pipeline(_ctx(store, redis, checkpointer), TID)) == "awaiting_gate"
    # revise round 1: attempt 2 of 2 — the graph re-suspends on the same gate
    assert asyncio.run(
        resume_pipeline(_ctx(store, redis, checkpointer), TID, "design",
                        {"approved": False, "feedback": "tighten scope"})
    ) == "awaiting_gate"
    assert store.tasks[TID]["current_gate"] == "design"
    # revise round 2: attempts exhausted → the surface path is a structural
    # stop (completed + rejected_design), never a spinning loop or an error
    assert asyncio.run(
        resume_pipeline(_ctx(store, redis, checkpointer), TID, "design",
                        {"approved": False, "feedback": "tighten scope"})
    ) == "rejected_design"
    row = store.tasks[TID]
    assert row["status"] == "completed" and row["outcome"] == "rejected_design"


def test_cancel_stops_at_superstep_boundary():
    _wire()
    store, redis = FakeWorkerStore(), FakeRedis()
    store.seed(TID, cancel_requested=True)

    assert asyncio.run(run_pipeline(_ctx(store, redis), TID)) == "cancelled"
    assert store.tasks[TID]["status"] == "cancelled"
    assert ("task.status", {"status": "cancelled"}) in [
        (event["type"], event["payload"]) for _, event in redis.events
    ]


def test_claim_skips_cancelled_task():
    _wire()
    store, redis = FakeWorkerStore(), FakeRedis()
    store.seed(TID, status="cancelled")
    assert asyncio.run(run_pipeline(_ctx(store, redis), TID)) == "skipped"
    assert store.tasks[TID]["status"] == "cancelled"


def test_retry_reenters_running_task():
    """An arq Retry redelivers the same job_id, which already holds 'running' —
    the claim must re-enter it instead of skipping the task into a stuck state
    (caught by the compose smoke test: LLM 401 on try=1, 'skipped' on try=2)."""
    _wire()
    store, redis = FakeWorkerStore(), FakeRedis()
    store.seed(TID)
    store._to(TID, "running")  # try=1 already claimed it, then failed transiently
    ctx = _ctx(store, redis)
    ctx["job_try"] = 2
    assert asyncio.run(run_pipeline(ctx, TID)) == "awaiting_gate"
    assert store.tasks[TID]["status"] == "awaiting_gate"


def test_resume_requires_matching_open_gate():
    _wire()
    store, redis = FakeWorkerStore(), FakeRedis()
    store.seed(TID)
    assert asyncio.run(run_pipeline(_ctx(store, redis), TID)) == "awaiting_gate"
    # a decision for the wrong gate cannot claim the task
    assert asyncio.run(
        resume_pipeline(_ctx(store, redis), TID, "cases", {"approved": True})
    ) == "skipped"
    assert store.tasks[TID]["status"] == "awaiting_gate"


def test_contract_failure_fails_task_without_retry():
    # The agent stamps its own envelope fields (version/kind/task_id) onto the
    # LLM output, so a contract breach must hit a required business field the
    # agent cannot fill: a missing coverage_matrix fails pydantic validation
    # inside the agent, and the boundary re-raises it as contract-fatal.
    broken_design = {k: v for k, v in DESIGN_DRAFT.items() if k != "coverage_matrix"}
    AGENT_REGISTRY["test_design"].llm = FakeLLM({"test-design agent": [broken_design]})
    AGENT_REGISTRY["whitebox"].llm = FakeLLM({"whitebox analysis agent": [dict(EVIDENCE)]})
    AGENT_REGISTRY["test_case"].llm = FakeLLM({"test-case agent": [dict(CASES)]})
    AGENT_REGISTRY["test_execution"].llm = FakeLLM({"test-execution agent": [dict(REPORT)]})

    store, redis = FakeWorkerStore(), FakeRedis()
    store.seed(TID)
    assert asyncio.run(run_pipeline(_ctx(store, redis), TID)) == "failed"
    row = store.tasks[TID]
    assert row["status"] == "failed" and row["error"].startswith("contract:")
    assert "task.failed" not in redis.types()  # failure surfaces via task.status
    failed_events = [event["payload"] for _, event in redis.events
                     if event["type"] == "task.status"]
    assert failed_events[-1]["status"] == "failed"
