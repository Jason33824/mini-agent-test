"""ARQ worker settings. W1 scaffold: job function placeholder; the real pipeline lands in W1 build-out / W3."""
import os

from arq.connections import RedisSettings


async def run_pipeline(ctx, task_id: str, user: str) -> dict:
    """Execute the 3-node graph for one submitted task.

    W3 adds: idempotency key, cooperative-cancel flag checks between nodes,
    per-node Phoenix spans, and token accounting against the user's quota.
    """
    raise NotImplementedError("pipeline lands in W1 build-out; see graph/")


class WorkerSettings:
    functions = [run_pipeline]
    redis_settings = RedisSettings(host=os.getenv("REDIS_HOST", "redis"))
    max_jobs = 4
    job_timeout = 600
    health_check_interval = 30
