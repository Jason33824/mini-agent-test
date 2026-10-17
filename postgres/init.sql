-- Task ledger schema. Mounted into the postgres container at first init
-- (docker-entrypoint-initdb.d runs it automatically on a fresh volume).
--
-- Design rule (HLD §7): the tasks table is the single source of truth for task
-- state. Redis carries queue signals (arq jobs) and progress fan-out (pub/sub)
-- only — losing either is recoverable; losing a task row is not.
--
-- users / quota_ledger tables land with the auth + quota batches; this schema
-- covers the task-execution core: tasks, gate_decisions, artifacts.

CREATE TABLE IF NOT EXISTS tasks (
    id               UUID PRIMARY KEY,
    user_id          TEXT NOT NULL,
    title            TEXT NOT NULL,
    -- State machine (LLD §10): each transition has exactly one owner — the
    -- worker owns queued→running / running→{awaiting_gate,completed,failed},
    -- the API owns cancel outcomes. Worker claims are conditional updates
    -- (WHERE status='queued'), so API cancel writes can never interleave.
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'awaiting_gate',
                                       'completed', 'failed', 'cancelled')),
    -- Which gate the task is suspended on while awaiting_gate (design/cases);
    -- NULL otherwise. Lets a snapshot reader (reconnected SSE client) know
    -- which decision form to render without replaying events.
    current_gate     TEXT,
    prd              TEXT NOT NULL,
    -- 'report' | 'rejected_design' | 'rejected_cases' (surface path is a
    -- structural stop, not an error: the pipeline ended, a human decides next)
    outcome          TEXT,
    error            TEXT,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks (user_id, created_at DESC);

-- One row per human gate decision: the audit trail behind the adoption-rate
-- metric (HLD §11.2) and the input for resume jobs.
CREATE TABLE IF NOT EXISTS gate_decisions (
    id         UUID PRIMARY KEY,
    task_id    UUID NOT NULL,
    gate       TEXT NOT NULL,
    approved   BOOLEAN NOT NULL,
    feedback   TEXT NOT NULL DEFAULT '',
    drop_areas JSONB NOT NULL DEFAULT '[]',  -- durable evidence veto (DESIGN.md §12)
    decided_by TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_task ON gate_decisions (task_id, decided_at);

-- Versioned artifacts, written once: the idempotency key dedupes crash-retry
-- replays; the attempt suffix keeps revise rounds as separate rows so the
-- final (approved) version is never lost.
CREATE TABLE IF NOT EXISTS artifacts (
    id              UUID PRIMARY KEY,
    task_id         UUID NOT NULL,
    kind            TEXT NOT NULL,
    version         TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    content         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts (task_id, created_at);
