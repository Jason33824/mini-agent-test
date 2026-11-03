-- Task ledger schema. Mounted into the postgres container at first init
-- (docker-entrypoint-initdb.d runs it automatically on a fresh volume).
--
-- Design rule (HLD §7): the tasks table is the single source of truth for task
-- state. Redis carries queue signals (arq jobs) and progress fan-out (pub/sub)
-- only — losing either is recoverable; losing a task row is not.
--
-- The auth + quota layer (users/tenants/quota_ledger) closes the loop at the
-- bottom of this file: login identities are seeded, usage is settled there.

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
    -- Stripe-style submission replay guard (ADR 0004); NULL = plain submission
    idempotency_key  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks (user_id, created_at DESC);
-- A retried POST carrying the same Idempotency-Key replays the original task
-- instead of creating a second one. Partial so NULL keys never collide, and
-- scoped per user so two tenants can pick the same key independently.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency
    ON tasks (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

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

-- Auth + quota layer (ADR 0003). A tenant is the quota namespace; users are
-- seeded so a fresh `docker compose up` is a usable system without a
-- registration flow. Demo passwords: alice-dev / bob-dev / carol-dev —
-- dev-only constants, stored as pbkdf2-hmac-sha256 (verified in api/auth.py).
CREATE TABLE IF NOT EXISTS tenants (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    -- per-calendar-month LLM-call budget; a task reserves the structural
    -- ceiling (6 calls) at submission and is settled at completion
    monthly_llm_calls INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id        TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    username  TEXT NOT NULL UNIQUE,
    pw_hash   TEXT NOT NULL  -- pbkdf2$sha256$<iters>$<salt-hex>$<hash-hex>
);

-- Usage ledger: one row per finished task, written by the worker. Unique on
-- (tenant, task) makes settlement idempotent — an arq Retry that re-drives
-- to the same terminal state cannot double-bill the tenant.
CREATE TABLE IF NOT EXISTS quota_ledger (
    id         UUID PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    task_id    UUID NOT NULL,
    llm_calls  INTEGER NOT NULL,
    settled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_quota_ledger_window
    ON quota_ledger (tenant_id, settled_at);

INSERT INTO tenants (id, name, monthly_llm_calls) VALUES
    ('acme',  'Acme QA',  60),   -- small seat: burns out visibly in a demo
    ('globex', 'Globex QA', 600)
ON CONFLICT DO NOTHING;
INSERT INTO users (id, tenant_id, username, pw_hash) VALUES
    ('alice', 'acme', 'alice',
     'pbkdf2$sha256$120000$9b0e9f91f1ac539266e0eb7541de2ab7$6ba3dda60a153b62ff9d567ca5ceac40994198150084d11a9adea104d090f7f7'),
    ('bob',   'acme', 'bob',
     'pbkdf2$sha256$120000$bb12e9c7a2921289d3937b73afbe66b3$ec310f96b7710f6fc5599e60576b7b57f027d4a534ddf8c28d10ac84ba123e7b'),
    ('carol', 'globex', 'carol',
     'pbkdf2$sha256$120000$e5fab5947df5be85c20521e99b2179a0$636ea7efbab23c3ea65476356ab105682c766c83ad15a4f5ab033fd05061a7dc')
ON CONFLICT DO NOTHING;
