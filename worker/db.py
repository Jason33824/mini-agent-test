"""Worker-side task store: lifecycle transitions only (LLD §10 ownership).

Deliberately narrow — no owner filters (only the worker touches tasks), no
CRUD. Every transition is a conditional UPDATE whose rowcount decides the
outcome, so worker claims and API cancel writes can never interleave badly.
"""
import json
import os
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def make_engine() -> AsyncEngine:
    url = os.environ["DATABASE_URL"]
    return create_async_engine(url, pool_pre_ping=True)


class WorkerStore:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    async def claim_runnable(self, task_id: str) -> dict[str, Any] | None:
        """queued/running → running (conditional claim).

        'running' is re-enterable by the same job only: arq Retry redelivers
        the same job_id (= task_id), which already holds the running state —
        excluding it would skip the retry forever. Anything cancelled/closed
        still falls through to a skip, so cancel semantics are unchanged.
        """
        row = await self._one(
            "UPDATE tasks SET status = 'running', updated_at = now()"
            " WHERE id = :task_id AND status IN ('queued', 'running')"
            " RETURNING id, prd",
            {"task_id": task_id},
        )
        return row

    async def claim_resuming(self, task_id: str, gate: str) -> dict[str, Any] | None:
        """awaiting_gate → running, only if the open gate matches the decision."""
        return await self._one(
            "UPDATE tasks SET status = 'running', updated_at = now()"
            " WHERE id = :task_id AND status = 'awaiting_gate' AND current_gate = :gate"
            " RETURNING id, prd",
            {"task_id": task_id, "gate": gate},
        )

    async def set_awaiting(self, task_id: str, gate: str) -> None:
        await self._exec(
            "UPDATE tasks SET status = 'awaiting_gate', current_gate = :gate,"
            " updated_at = now() WHERE id = :task_id",
            {"task_id": task_id, "gate": gate},
        )

    async def cancel_requested(self, task_id: str) -> bool:
        row = await self._one(
            "SELECT cancel_requested FROM tasks WHERE id = :task_id", {"task_id": task_id}
        )
        return bool(row and row["cancel_requested"])

    async def mark_cancelled(self, task_id: str) -> None:
        await self._exec(
            "UPDATE tasks SET status = 'cancelled', updated_at = now() WHERE id = :task_id",
            {"task_id": task_id},
        )

    async def complete(self, task_id: str, outcome: str) -> None:
        await self._exec(
            "UPDATE tasks SET status = 'completed', outcome = :outcome, updated_at = now()"
            " WHERE id = :task_id",
            {"task_id": task_id, "outcome": outcome},
        )

    async def fail(self, task_id: str, error: str) -> None:
        await self._exec(
            "UPDATE tasks SET status = 'failed', error = :error, updated_at = now()"
            " WHERE id = :task_id",
            {"task_id": task_id, "error": error[:2000]},
        )

    async def save_artifacts(self, task_id: str, values: dict[str, Any]) -> list[tuple[str, str]]:
        """Persist final artifacts, written once per (kind, attempt).

        The agents' own idempotency_key ({task_id}:{kind}) dedupes crash-retry
        replays; the attempt suffix separates revise rounds so the final
        approved version is retained instead of being swallowed by the conflict
        clause. Returns [(kind, artifact_id)] for newly inserted rows.
        """
        attempts = {
            "design": "design_attempts",
            "evidence": None,
            "cases": "cases_attempts",
            "report": "cases_attempts",
        }
        saved: list[tuple[str, str]] = []
        async with self._engine.begin() as conn:
            for field, kind in (("design", "design"), ("evidence", "evidence"),
                                ("cases", "cases"), ("report", "report")):
                content = values.get(field)
                if not content:
                    continue
                attempt_key = attempts[field]
                attempt = (values.get(attempt_key) if attempt_key else None) or 1
                base_key = content.get("idempotency_key", f"{task_id}:{kind}")
                idem = f"{base_key}:a{attempt}"
                row = await conn.execute(
                    text(
                        "INSERT INTO artifacts (id, task_id, kind, version, idempotency_key, content)"
                        " VALUES (gen_random_uuid(), :task_id, :kind, :version, :idem, CAST(:content AS jsonb))"
                        " ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"
                    ),
                    {
                        "task_id": task_id, "kind": kind,
                        "version": content.get("version", "v1"),
                        "idem": idem, "content": json.dumps(content),
                    },
                )
                new_id = row.scalar()
                if new_id:
                    saved.append((kind, str(new_id)))
        return saved

    async def _one(self, sql: str, params: dict) -> dict[str, Any] | None:
        async with self._engine.begin() as conn:
            row = (await conn.execute(text(sql), params)).mappings().first()
        return dict(row) if row else None

    async def _exec(self, sql: str, params: dict) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(text(sql), params)
