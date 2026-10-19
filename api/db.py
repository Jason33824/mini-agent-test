"""Task-store access for the API side: owner-scoped reads + lifecycle writes.

Module boundary (LLD §1): api owns task CRUD and the gate-decision ingress and
never runs the graph. The worker has its own narrower store (worker/db.py) —
the two sides deliberately do not share a package: separate container images,
different query semantics (api filters by owner, worker by lifecycle).
"""
import json
import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def make_engine() -> AsyncEngine:
    url = os.environ["DATABASE_URL"]  # fail fast at import: compose always sets it
    return create_async_engine(url, pool_pre_ping=True)


class TaskStore:
    """All SQL the API is allowed to speak. One class so tests can fake it."""

    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(text("SELECT 1"))

    async def create_task(self, user_id: str, title: str, prd: str) -> dict[str, Any]:
        sql = text(
            "INSERT INTO tasks (id, user_id, title, prd)"
            " VALUES (gen_random_uuid(), :user_id, :title, :prd) RETURNING *"
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(sql, {"user_id": user_id, "title": title, "prd": prd})).mappings().one()
        return dict(row)

    async def list_tasks(
        self, user_id: str, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks WHERE user_id = :user_id"
        params: dict[str, Any] = {"user_id": user_id, "limit": limit, "offset": offset}
        if status:
            sql += " AND status = :status"
            params["status"] = status
        sql += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        async with self._engine.begin() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]

    async def get_task(self, task_id: str, user_id: str) -> dict[str, Any] | None:
        """Owner-scoped read: a miss is a 404 — never a 403 (LLD §13)."""
        sql = text("SELECT * FROM tasks WHERE id = :task_id AND user_id = :user_id")
        async with self._engine.begin() as conn:
            row = (await conn.execute(sql, {"task_id": task_id, "user_id": user_id})).mappings().first()
        return dict(row) if row else None

    async def list_artifacts(self, task_id: str, user_id: str) -> list[dict[str, Any]]:
        """Owner-scoped artifact listing (joined through the task's owner)."""
        sql = text(
            "SELECT a.id, a.task_id, a.kind, a.version, a.idempotency_key,"
            " a.content, a.created_at FROM artifacts a"
            " JOIN tasks t ON t.id = a.task_id"
            " WHERE a.task_id = :task_id AND t.user_id = :user_id"
            " ORDER BY a.created_at"
        )
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(sql, {"task_id": task_id, "user_id": user_id})
            ).mappings().all()
        return [dict(r) for r in rows]

    async def record_decision(
        self, task_id: str, gate: str, approved: bool, feedback: str,
        drop_areas: list[str], decided_by: str,
    ) -> str:
        sql = text(
            "INSERT INTO gate_decisions (id, task_id, gate, approved, feedback,"
            " drop_areas, decided_by)"
            " VALUES (gen_random_uuid(), :task_id, :gate, :approved, :feedback,"
            " CAST(:drop_areas AS jsonb), :decided_by) RETURNING id"
        )
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(sql, {
                    "task_id": task_id, "gate": gate, "approved": approved,
                    "feedback": feedback, "drop_areas": json.dumps(drop_areas),
                    "decided_by": decided_by,
                })
            ).scalar_one()
        return str(row)

    async def cancel(self, task_id: str, current_status: str) -> str:
        """Three-way cancel semantics (LLD §10 transition table).

        queued/awaiting_gate → the API owns the cancelled transition: flip now.
        running              → the worker owns terminal transitions: set the
                               cooperative flag; the graph stops at the next
                               superstep boundary.
        terminal             → conflict; caller turns it into a 409.
        """
        if current_status == "running":
            sql = text(
                "UPDATE tasks SET cancel_requested = TRUE, updated_at = now()"
                " WHERE id = :task_id AND status = 'running'"
            )
            outcome = "cancel_requested"
        elif current_status in ("queued", "awaiting_gate"):
            sql = text(
                "UPDATE tasks SET status = 'cancelled', cancel_requested = TRUE,"
                " updated_at = now() WHERE id = :task_id AND status = :expected"
            )
            outcome = "cancelled"
        else:
            return "conflict"
        async with self._engine.begin() as conn:
            result = await conn.execute(sql, {"task_id": task_id, "expected": current_status})
        return outcome if result.rowcount else "conflict"
