"""FastAPI entrypoint: task lifecycle, gate-decision ingress, SSE fan-out.

Route surface follows the LLD (§8). JWT auth lands in its own batch — until
then identity comes from the X-User-Id header (default "local"); route shapes
stay identical, only the identity source moves.

SSE semantics: events are relayed from the worker's redis pub/sub channel
task:{task_id}. There is no event log — a client that reconnects after missing
events takes a fresh snapshot via GET /v1/tasks/{id} and re-subscribes (LLD §15
frontend pattern). The stream ends on the first terminal transition.
"""
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from api.db import TaskStore, make_engine

logger = logging.getLogger("api")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
MAX_PRD_BYTES = 64 * 1024  # LLD §13: PRD size cap
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class ApiError(Exception):
    """Unified error envelope: {"error": {code, message, details}} (LLD §8)."""

    def __init__(self, status_code: int, code: str, message: str, **details: Any):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = TaskStore(make_engine())
    app.state.redis = aioredis.from_url(REDIS_URL)
    app.state.queue = await create_pool(RedisSettings.from_dsn(REDIS_URL))
    logger.info("api ready: db+redis+arq pool up")
    yield
    await app.state.queue.aclose()
    await app.state.redis.aclose()
    await app.state.store.close()


app = FastAPI(title="mini-ai-test-agent-platform", version="0.2.0", lifespan=lifespan)


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


def get_store(request: Request) -> TaskStore:
    return request.app.state.store


def get_redis(request: Request):
    return request.app.state.redis


def get_queue(request: Request):
    return request.app.state.queue


# Route dependencies resolve from app.state (wired in lifespan); tests swap
# them wholesale via dependency_overrides — the seam the offline suites ride.
StoreDep = Annotated[TaskStore, Depends(get_store)]
RedisDep = Annotated[Any, Depends(get_redis)]
QueueDep = Annotated[Any, Depends(get_queue)]


async def current_user(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """Identity until the JWT batch: X-User-Id header, default "local"."""
    return x_user_id or "local"


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    prd: str = Field(min_length=1)


class GateDecisionIn(BaseModel):
    approved: bool
    feedback: str = ""
    drop_areas: list[str] = Field(default_factory=list)


def _channel(task_id: str) -> str:
    return f"task:{task_id}"


async def _publish(redis, task_id: str, type_: str, payload: dict) -> None:
    event = {
        "type": type_,
        "payload": payload,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    await redis.publish(_channel(task_id), json.dumps(event))


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "api"}


@app.get("/readyz")
async def readyz(request: Request) -> dict:
    await request.app.state.store.ping()
    await request.app.state.redis.ping()
    return {"status": "ready"}


@app.post("/v1/tasks", status_code=202)
async def submit_task(
    body: TaskIn,
    user: Annotated[str, Depends(current_user)],
    store: StoreDep,
    queue: QueueDep,
    redis: RedisDep,
) -> dict:
    if len(body.prd.encode("utf-8")) > MAX_PRD_BYTES:
        raise ApiError(400, "prd_too_large", f"prd exceeds {MAX_PRD_BYTES} bytes")
    task = await store.create_task(user, body.title, body.prd)
    task_id = str(task["id"])
    # job_id = task_id → duplicate enqueues are no-ops (queue-level idempotency)
    await queue.enqueue_job("run_pipeline", task_id, _job_id=task_id)
    await _publish(redis, task_id, "task.status", {"status": "queued"})
    return {"task_id": task_id, "status": "queued"}


@app.get("/v1/tasks")
async def list_tasks(
    user: Annotated[str, Depends(current_user)],
    store: StoreDep,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    tasks = await store.list_tasks(user, status, min(limit, 200), max(offset, 0))
    return {"tasks": tasks}


@app.get("/v1/tasks/{task_id}")
async def get_task(
    task_id: str,
    user: Annotated[str, Depends(current_user)],
    store: StoreDep,
) -> dict:
    task = await store.get_task(task_id, user)
    if task is None:
        raise ApiError(404, "task_not_found", "no such task for this user")
    return {"task": task}


@app.post("/v1/tasks/{task_id}/gates/{gate}", status_code=202)
async def decide_gate(
    task_id: str,
    gate: str,
    body: GateDecisionIn,
    user: Annotated[str, Depends(current_user)],
    store: StoreDep,
    queue: QueueDep,
) -> dict:
    task = await store.get_task(task_id, user)
    if task is None:
        raise ApiError(404, "task_not_found", "no such task for this user")
    if task["status"] != "awaiting_gate" or task["current_gate"] != gate:
        raise ApiError(
            409, "invalid_transition",
            f"task is {task['status']!r} (gate {task['current_gate']!r});"
            f" cannot decide gate {gate!r}",
        )
    decision_id = await store.record_decision(
        task_id, gate, body.approved, body.feedback, body.drop_areas, user
    )
    # The decision id makes the resume job id replay-proof: re-enqueuing the
    # same decision is a no-op, a new decision gets a fresh job (LLD §11).
    await queue.enqueue_job(
        "resume_pipeline", task_id, gate, body.model_dump(),
        _job_id=f"{task_id}:resume:{decision_id}",
    )
    return {"task_id": task_id, "gate": gate, "decision_id": decision_id, "status": "resuming"}


@app.post("/v1/tasks/{task_id}/cancel", status_code=202)
async def cancel_task(
    task_id: str,
    user: Annotated[str, Depends(current_user)],
    store: StoreDep,
    redis: RedisDep,
) -> dict:
    task = await store.get_task(task_id, user)
    if task is None:
        raise ApiError(404, "task_not_found", "no such task for this user")
    outcome = await store.cancel(task_id, task["status"])
    if outcome == "conflict":
        raise ApiError(409, "already_terminal", f"task already {task['status']}")
    if outcome == "cancelled":
        await _publish(redis, task_id, "task.status", {"status": "cancelled"})
    # "cancel_requested": the running node finishes; the graph stops at the
    # next superstep boundary — cancellation never cuts an LLM call mid-flight.
    return {"task_id": task_id, "result": outcome}


@app.get("/v1/tasks/{task_id}/events")
async def task_events(
    task_id: str,
    user: Annotated[str, Depends(current_user)],
    store: StoreDep,
    redis: RedisDep,
) -> StreamingResponse:
    task = await store.get_task(task_id, user)
    if task is None:
        raise ApiError(404, "task_not_found", "no such task for this user")
    return StreamingResponse(
        _event_stream(redis, task_id),
        media_type="text/event-stream",
    )


async def _event_stream(redis, task_id: str):
    """Relay pub/sub → SSE wire format; end the stream on a terminal transition."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(_channel(task_id))
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
            if message is None:
                yield ": keepalive\n\n"
                continue
            event = json.loads(message["data"])
            yield f"event: {event['type']}\ndata: {json.dumps(event['payload'])}\n\n"
            status = event["payload"].get("status")
            if event["type"] == "task.completed" or status in TERMINAL_STATUSES:
                break
    finally:
        await pubsub.unsubscribe(_channel(task_id))
        await pubsub.aclose()
