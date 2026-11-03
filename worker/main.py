"""ARQ worker: the only place the orchestrator graph is executed (LLD §11).

Production semantics implemented here — each one is observable from the outside:

- job_id = task_id: duplicate enqueues are no-ops (queue-level idempotency)
- claim = conditional UPDATE ... WHERE status IN ('queued','running'): race-free
  against the API's cancel writes (single-writer state machine, LLD §10);
  'running' is re-enterable by the same job only — an arq Retry redelivers the
  same job_id (= task_id), which already holds the running state
- the graph runs under a Postgres checkpointer with thread_id = user:task
  namespacing (the checkpoint stream never shares a thread with another user),
  so a gate interrupt survives process restarts; resume = Command(resume=decision)
- quota settlement: a finished task books its real LLM call count to the
  tenant ledger — the count is exact because every handoff makes exactly one
  call (DESIGN.md §11), readable straight off the final state
- cooperative cancellation: cancel_requested is checked at every superstep
  boundary of graph.stream() — the running node finishes, the next never starts
- transient errors → arq Retry (exponential 2**attempt); contract-fatal
  ValueError (artifact schema / link_check) → failed, never retried
"""
import asyncio
import json
import logging
import os
import threading
from datetime import UTC, datetime
from typing import Any, ClassVar

import redis.asyncio as aioredis
from arq import Retry
from arq.connections import RedisSettings
from langgraph.types import Command
from opentelemetry import context as otel_context

from orchestrator import build_orchestrator
from tracing import span
from worker.db import WorkerStore, make_engine

logger = logging.getLogger("worker")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
MAX_JOB_TRIES = 3


class TaskCancelled(Exception):
    """Raised internally when the cooperative cancel flag was observed."""


def _channel(task_id: str) -> str:
    return f"task:{task_id}"


async def _publish(redis, task_id: str, type_: str, payload: dict) -> None:
    event = {
        "type": type_,
        "payload": payload,
        "ts": datetime.now(UTC).isoformat(),
    }
    await redis.publish(_channel(task_id), json.dumps(event))


def _parse_chunk(chunk: Any) -> tuple[str | None, dict | None]:
    """Split one stream chunk into (node_name, interrupt_value).

    langgraph surfaces interrupts in updates mode as {"__interrupt__": (...)}
    (tuple form ("__interrupt__", ...) on some mode combinations) — normalize
    both shapes so the worker is insensitive to the pinned minor version.
    """
    if isinstance(chunk, dict):
        if "__interrupt__" in chunk:
            items = chunk["__interrupt__"]
        else:
            return (next(iter(chunk), None), None)
    elif isinstance(chunk, tuple) and len(chunk) == 2 and chunk[0] == "__interrupt__":
        items = chunk[1]
    else:
        return None, None
    first = items[0] if isinstance(items, (list, tuple)) and items else items
    value = getattr(first, "value", None)
    return None, value if isinstance(value, dict) else {}


# gate stage → state field holding the artifact a human needs to review
_GATE_ARTIFACT_FIELD = {"design": "design", "cases": "cases"}


def _llm_calls(values: dict[str, Any]) -> int:
    """Exact per-task LLM call count from the final graph state.

    Every handoff makes exactly one call and bumps its attempt counter; the
    whitebox branch has no counter (it never revises) but runs at most once,
    proven by the presence of its artifact. design + cases-attempts (which
    also counts the execution handoff) + whitebox = the per-task total.
    """
    return (
        values.get("design_attempts", 0)
        + values.get("cases_attempts", 0)
        + (1 if values.get("evidence") else 0)
    )


async def _drive_graph(ctx, task_id: str, user_id: str, graph_input: Any) -> str:
    """Stream one graph execution; translate superstep boundaries into state
    transitions, gate suspensions and pub/sub events."""
    store: WorkerStore = ctx["store"]
    redis = ctx["redis"]
    graph = build_orchestrator(checkpointer=ctx["checkpointer"])
    config = {"configurable": {"thread_id": f"{user_id}:{task_id}"}}

    # The graph is sync; drive it in a thread and hand chunks to the event loop
    # through a queue. Each chunk boundary doubles as a cancel checkpoint.
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    # OTel context is per-thread; snapshot it (the task span is current here)
    # and re-attach inside the producer, so handoff/llm spans nest under the
    # task span instead of floating as separate traces
    graph_ctx = otel_context.get_current()

    def _emit(kind: str, item: Any) -> None:
        # The async side can be gone before this thread is (test bypasses,
        # teardown races): a closed loop means nobody will read the chunk,
        # so drop it instead of raising inside the producer.
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, item))
        except RuntimeError:  # loop closed between check and call
            pass

    def _produce() -> None:
        token = otel_context.attach(graph_ctx)
        try:
            for chunk in graph.stream(graph_input, config, stream_mode="updates"):
                _emit("chunk", chunk)
            values = graph.get_state(config).values
            _emit("state", values)
        except BaseException as exc:  # noqa: BLE001 — relayed to the async side
            _emit("error", exc)
        finally:
            otel_context.detach(token)

    threading.Thread(target=_produce, daemon=True, name=f"graph-{task_id}").start()

    pending_gate: str | None = None
    while True:
        kind, item = await queue.get()
        if kind == "error":
            raise item
        if kind == "chunk":
            if await store.cancel_requested(task_id):
                await store.mark_cancelled(task_id)
                await _publish(redis, task_id, "task.status", {"status": "cancelled"})
                raise TaskCancelled(task_id)
            _, gate_value = _parse_chunk(item)
            if gate_value is not None:
                pending_gate = gate_value.get("stage") or "unknown"
            # node-level progress stays in checkpoint + trace; the SSE surface
            # only carries the transitions the LLD event vocabulary defines
            continue
        # kind == "state": stream finished — either suspended on a gate or done
        values = item or {}
        if pending_gate is not None:
            await store.set_awaiting(task_id, pending_gate)
            artifact = values.get(_GATE_ARTIFACT_FIELD.get(pending_gate, ""))
            await _publish(redis, task_id, "gate.request", {
                "v": 1, "stage": pending_gate, "task_id": task_id, "artifact": artifact,
            })
            return "awaiting_gate"
        outcome = values.get("outcome", "report")
        for kind_, artifact_id in await store.save_artifacts(task_id, values):
            await _publish(redis, task_id, "artifact.created",
                           {"kind": kind_, "artifact_id": artifact_id})
        # book before completing: a crash between the two re-settles on Retry
        # idempotently (ledger unique on tenant+task); failed/cancelled runs
        # are not billed — the ledger records finished work, so the worst
        # case is conservative under-counting, never double-charging
        await store.settle_quota(task_id, _llm_calls(values))
        await store.complete(task_id, outcome)
        await _publish(redis, task_id, "task.status", {"status": "completed", "outcome": outcome})
        if outcome == "report":
            await _publish(redis, task_id, "task.completed", {"outcome": outcome})
        return outcome


async def _execute(ctx, task_id: str, user_id: str, graph_input: Any) -> str:
    """Shared error policy around one graph drive (run + resume)."""
    store: WorkerStore = ctx["store"]
    redis = ctx["redis"]
    try:
        return await _drive_graph(ctx, task_id, user_id, graph_input)
    except TaskCancelled:
        return "cancelled"
    except ValueError as exc:
        # Contract-fatal (artifact schema / link_check): a retry would fail the
        # same way — fail the task with the typed error (LLD §11).
        logger.warning("task %s failed on contract: %s", task_id, exc)
        await store.fail(task_id, f"contract: {exc}")
        await _publish(redis, task_id, "task.status", {"status": "failed", "error": str(exc)})
        return "failed"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if ctx.get("job_try", 1) >= MAX_JOB_TRIES:
            logger.error("task %s exhausted retries: %s", task_id, exc)
            await store.fail(task_id, f"exhausted: {exc}")
            await _publish(redis, task_id, "task.status", {"status": "failed", "error": str(exc)})
            return "failed"
        # Checkpoint makes the redelivery a continuation, not a rerun.
        # arq's Retry takes `defer` (seconds to wait before the next attempt).
        raise Retry(defer=2 ** ctx.get("job_try", 1)) from exc


async def run_pipeline(ctx, task_id: str) -> str:
    """One runnable task → full graph execution (or suspension at a gate)."""
    store: WorkerStore = ctx["store"]
    task = await store.claim_runnable(task_id)
    if task is None:
        # cancelled/closed before claim, or a duplicate delivery the job_id
        # dedupe already collapsed — nothing to do either way
        return "skipped"
    await _publish(ctx["redis"], task_id, "task.status", {"status": "running"})
    with span("task.run", {"task.id": task_id, "user.id": task["user_id"]}, kind="CHAIN"):
        return await _execute(ctx, task_id, task["user_id"], {"task_id": task_id, "prd": task["prd"]})


async def resume_pipeline(ctx, task_id: str, gate: str, decision: dict) -> str:
    """A recorded gate decision → continue the graph from its checkpoint."""
    store: WorkerStore = ctx["store"]
    task = await store.claim_resuming(task_id, gate)
    if task is None:
        return "skipped"  # decision superseded by a cancel, or duplicate delivery
    await _publish(ctx["redis"], task_id, "task.status", {"status": "running"})
    with span("task.resume", {"task.id": task_id, "user.id": task["user_id"],
                              "gate": gate}, kind="CHAIN"):
        return await _execute(ctx, task_id, task["user_id"], Command(resume=decision))


async def on_startup(ctx) -> None:
    ctx["store"] = WorkerStore(make_engine())
    ctx["redis"] = aioredis.from_url(REDIS_URL)
    # Checkpoint backend import is deferred on purpose: tests inject an
    # InMemorySaver through ctx and never import the postgres saver here.
    from langgraph.checkpoint.postgres import PostgresSaver

    connection_cm = PostgresSaver.from_conn_string(
        os.environ["DATABASE_URL"].replace("+psycopg", "")
    )
    saver = connection_cm.__enter__()  # process lifetime = checkpoint lifetime
    saver.setup()  # idempotent DDL for the checkpoint tables
    ctx["checkpointer"] = saver
    ctx["_checkpoint_cm"] = connection_cm
    logger.info("worker ready: store+redis+checkpointer up")


async def on_shutdown(ctx) -> None:
    cm = ctx.get("_checkpoint_cm")
    if cm is not None:
        cm.__exit__(None, None, None)
    await ctx["redis"].aclose()
    await ctx["store"].close()


class WorkerSettings:
    functions: ClassVar[list] = [run_pipeline, resume_pipeline]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    max_jobs = 4
    job_timeout = 600
    max_tries = MAX_JOB_TRIES
    health_check_interval = 30
