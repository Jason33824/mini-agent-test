"""FastAPI entrypoint: auth ingress, task lifecycle, gate-decision ingress, SSE fan-out.

Route surface follows the LLD (§8). Identity is a bearer JWT (ADR 0003):
/v1/auth/token exchanges seeded credentials for a short-lived token, and every
/v1 route resolves the Principal from that token — anonymous requests are 401
before they touch the queue. Quota (DESIGN.md §11) is enforced at submission:
the tenant's remaining monthly budget must cover the task's *structural* LLM
call ceiling, and the real usage lands in the ledger at completion.

SSE semantics: events are relayed from the worker's redis pub/sub channel
task:{task_id}. There is no event log — a client that reconnects after missing
events takes a fresh snapshot via GET /v1/tasks/{id} and re-subscribes (LLD §15
frontend pattern). The stream ends on the first terminal transition.
"""
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

import jwt
import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from api.auth import Principal, create_token, decode_token, verify_password
from api.db import TaskStore, make_engine

logger = logging.getLogger("api")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
MAX_PRD_BYTES = 64 * 1024  # LLD §13: PRD size cap
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
# Reservation per submitted task: the orchestrator's structural call ceiling
# (DESIGN.md §11 — bounded revise makes worst-case exact, not optimistic).
# Mirrors the bound pinned by test_llm_call_count_is_bounded; the graph owns
# the math, the quota layer only quotes it.
MAX_LLM_CALLS_PER_TASK = 6


def _window_reset() -> tuple[int, int]:
    """(unix ts, seconds) of the next calendar month — the quota window boundary.

    x-ratelimit-reset follows the OpenAI convention (unix timestamp when the
    counter resets); retry-after follows RFC 9110 (delta seconds).
    """
    now = datetime.now(UTC)
    y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    nxt = datetime(y, m, 1, tzinfo=UTC)
    return int(nxt.timestamp()), max(1, int((nxt - now).total_seconds()))


class ApiError(Exception):
    """Unified error envelope: {"error": {code, message, details}} (LLD §8)."""

    def __init__(self, status_code: int, code: str, message: str,
                 headers: dict[str, str] | None = None, **details: Any):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}
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


app = FastAPI(title="mini-agent-test", version="0.2.0", lifespan=lifespan)


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        headers=exc.headers,
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


async def principal(request: Request) -> Principal:
    """Bearer JWT → Principal on every /v1 route; anything else is a 401.

    Decoding is stateless (ADR 0003): an expired or tampered token fails here,
    never reaching the store or the queue.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise ApiError(401, "unauthorized", "missing bearer token")
    try:
        return decode_token(header.removeprefix("Bearer ").strip())
    except jwt.InvalidTokenError as exc:
        raise ApiError(401, "unauthorized", f"invalid token: {type(exc).__name__}")


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


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
        "ts": datetime.now(UTC).isoformat(),
    }
    await redis.publish(_channel(task_id), json.dumps(event))


PrincipalDep = Annotated[Principal, Depends(principal)]


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "api"}


@app.get("/readyz")
async def readyz(request: Request) -> dict:
    await request.app.state.store.ping()
    await request.app.state.redis.ping()
    return {"status": "ready"}


@app.post("/v1/auth/token")
async def login(body: LoginIn, store: StoreDep) -> dict:
    """Credentials → short-lived JWT. One message for unknown username and
    wrong password — login must not be a user enumeration oracle."""
    row = await store.authenticate(body.username)
    if row is None or not verify_password(body.password, row["pw_hash"]):
        raise ApiError(401, "invalid_credentials", "wrong username or password")
    return {
        "access_token": create_token(row["id"], row["tenant_id"]),
        "token_type": "bearer",
        "user_id": row["id"],
        "tenant_id": row["tenant_id"],
        "expires_in": 12 * 3600,
    }


@app.post("/v1/tasks", status_code=202)
async def submit_task(
    request: Request,
    response: Response,
    body: TaskIn,
    who: PrincipalDep,
    store: StoreDep,
    queue: QueueDep,
    redis: RedisDep,
) -> dict:
    if len(body.prd.encode("utf-8")) > MAX_PRD_BYTES:
        raise ApiError(400, "prd_too_large", f"prd exceeds {MAX_PRD_BYTES} bytes")
    idem = (request.headers.get("Idempotency-Key") or "").strip()[:256] or None
    quota = await store.quota_status(who.tenant_id)
    reset_ts, reset_in = _window_reset()
    # OpenAI-style rate headers (IETF draft-rate-limit-fields): remaining is
    # what still fits *after* this submission's worst-case reservation
    remaining = max(0, quota["limit"] - quota["used"] - MAX_LLM_CALLS_PER_TASK)
    rate = {
        "x-ratelimit-limit": str(quota["limit"]),
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-reset": str(reset_ts),
    }
    if quota["used"] + MAX_LLM_CALLS_PER_TASK > quota["limit"]:
        # reservation semantics: the *worst-case* task must fit the remaining
        # budget — overcommit would let parallel submissions blow past the cap
        raise ApiError(
            429, "quota_exceeded",
            f"tenant monthly budget exhausted ({quota['used']}/{quota['limit']} calls used)",
            headers={**rate,
                     "retry-after": str(reset_in),
                     "x-quota-used": str(quota["used"]),
                     "x-quota-limit": str(quota["limit"])},
        )
    task, replayed = await store.create_task(who.user_id, body.title, body.prd, idem)
    response.headers.update(rate)
    task_id = str(task["id"])
    if replayed:
        # Stripe pattern (ADR 0004): the same key replays the original task —
        # no second row, no second enqueue; the header marks the response as a
        # replay so a retried client can tell it apart from a fresh accept.
        response.headers["idempotent-replayed"] = "true"
        return {"task_id": task_id, "status": task["status"]}
    # job_id = task_id → duplicate enqueues are no-ops (queue-level idempotency)
    await queue.enqueue_job("run_pipeline", task_id, _job_id=task_id)
    await _publish(redis, task_id, "task.status", {"status": "queued"})
    return {"task_id": task_id, "status": "queued"}


@app.get("/v1/tasks")
async def list_tasks(
    who: PrincipalDep,
    store: StoreDep,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    tasks = await store.list_tasks(who.user_id, status, min(limit, 200), max(offset, 0))
    return {"tasks": tasks}


@app.get("/v1/tasks/{task_id}")
async def get_task(
    task_id: str,
    who: PrincipalDep,
    store: StoreDep,
) -> dict:
    task = await store.get_task(task_id, who.user_id)
    if task is None:
        raise ApiError(404, "task_not_found", "no such task for this user")
    return {"task": task}


@app.post("/v1/tasks/{task_id}/gates/{gate}", status_code=202)
async def decide_gate(
    task_id: str,
    gate: str,
    body: GateDecisionIn,
    who: PrincipalDep,
    store: StoreDep,
    queue: QueueDep,
) -> dict:
    task = await store.get_task(task_id, who.user_id)
    if task is None:
        raise ApiError(404, "task_not_found", "no such task for this user")
    if task["status"] != "awaiting_gate" or task["current_gate"] != gate:
        raise ApiError(
            409, "invalid_transition",
            f"task is {task['status']!r} (gate {task['current_gate']!r});"
            f" cannot decide gate {gate!r}",
        )
    decision_id = await store.record_decision(
        task_id, gate, body.approved, body.feedback, body.drop_areas, who.user_id
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
    who: PrincipalDep,
    store: StoreDep,
    redis: RedisDep,
) -> dict:
    task = await store.get_task(task_id, who.user_id)
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


@app.get("/v1/tasks/{task_id}/artifacts")
async def list_task_artifacts(
    task_id: str,
    who: PrincipalDep,
    store: StoreDep,
) -> dict:
    """Persisted artifact ledger (LLD §9): the frontend renders report cards
    from this after completion — SSE only carries the artifact id, not content."""
    task = await store.get_task(task_id, who.user_id)
    if task is None:
        raise ApiError(404, "task_not_found", "no such task for this user")
    return {"artifacts": await store.list_artifacts(task_id, who.user_id)}


@app.get("/v1/tasks/{task_id}/events")
async def task_events(
    task_id: str,
    who: PrincipalDep,
    store: StoreDep,
    redis: RedisDep,
) -> StreamingResponse:
    task = await store.get_task(task_id, who.user_id)
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
