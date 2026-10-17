"""ARQ worker: the only place the orchestrator graph is executed (LLD §11).

Production semantics implemented here — each one is observable from the outside:

- job_id = task_id: duplicate enqueues are no-ops (queue-level idempotency)
- claim = conditional UPDATE ... WHERE status IN ('queued','running'): race-free
  against the API's cancel writes (single-writer state machine, LLD §10);
  'running' is re-enterable by the same job only — an arq Retry redelivers the
  same job_id (= task_id), which already holds the running state
- the graph runs under a Postgres checkpointer with thread_id = task_id, so a
  gate interrupt survives process restarts; resume = Command(resume=decision)
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
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from arq import Retry
from arq.connections import RedisSettings
from langgraph.types import Command

from orchestrator import build_orchestrator
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
        "ts": datetime.now(timezone.utc).isoformat(),
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


async def _drive_graph(ctx, task_id: str, graph_input: Any) -> str:
    """Stream one graph execution; translate superstep boundaries into state
    transitions, gate suspensions and pub/sub events."""
    store: WorkerStore = ctx["store"]
    redis = ctx["redis"]
    graph = build_orchestrator(checkpointer=ctx["checkpointer"])
    config = {"configurable": {"thread_id": task_id}}

    # The graph is sync; drive it in a thread and hand chunks to the event loop
    # through a queue. Each chunk boundary doubles as a cancel checkpoint.
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _produce() -> None:
        try:
            for chunk in graph.stream(graph_input, config, stream_mode="updates"):
                loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
            values = graph.get_state(config).values
            loop.call_soon_threadsafe(queue.put_nowait, ("state", values))
        except BaseException as exc:  # noqa: BLE001 — relayed to the async side
            loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))

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
            node, gate_value = _parse_chunk(item)
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
        await store.complete(task_id, outcome)
        await _publish(redis, task_id, "task.status", {"status": "completed", "outcome": outcome})
        if outcome == "report":
            await _publish(redis, task_id, "task.completed", {"outcome": outcome})
        return outcome


async def _execute(ctx, task_id: str, graph_input: Any) -> str:
    """Shared error policy around one graph drive (run + resume)."""
    store: WorkerStore = ctx["store"]
    redis = ctx["redis"]
    try:
        return await _drive_graph(ctx, task_id, graph_input)
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
    except Exception as exc:  # noqa: BLE001 — transient policy decided here
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
    return await _execute(ctx, task_id, {"task_id": task_id, "prd": task["prd"]})


async def resume_pipeline(ctx, task_id: str, gate: str, decision: dict) -> str:
    """A recorded gate decision → continue the graph from its checkpoint."""
    store: WorkerStore = ctx["store"]
    task = await store.claim_resuming(task_id, gate)
    if task is None:
        return "skipped"  # decision superseded by a cancel, or duplicate delivery
    await _publish(ctx["redis"], task_id, "task.status", {"status": "running"})
    return await _execute(ctx, task_id, Command(resume=decision))


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
    functions = [run_pipeline, resume_pipeline]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    max_jobs = 4
    job_timeout = 600
    max_tries = MAX_JOB_TRIES
    health_check_interval = 30
