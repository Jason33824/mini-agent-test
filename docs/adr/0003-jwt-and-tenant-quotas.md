# ADR 0003: JWT auth + per-tenant quotas — stateless identity, reserved cost

- **Status:** accepted
- **Date:** 2026-11-03
- **Extends:** ADR 0001 (container topology), DESIGN.md §11 (what stays out of the graph)

## Context

The route surface needs an identity that survives the api/worker split: the
API authenticates a human, the worker executes a graph, and the queue between
them must not carry credentials. Cost control needs a tenant the budget
attaches to — a per-user budget would re-derive grouping policy inside every
service, while the natural accounting unit (who pays for the LLM calls) is an
organization.

## Decision

1. **Stateless HS256 bearer tokens** (`api/auth.py`): `/v1/auth/token`
   exchanges seeded credentials for a 12-hour token carrying `sub` (user id)
   and `tenant`. No session store; the worker never sees a token — it trusts
   the `user_id` the API persisted on the task row at submission.
2. **Passwords: pbkdf2-hmac-sha256, stdlib only.** Self-describing stored
   format (`pbkdf2$sha256$<iters>$<salt>$<digest>`), constant-time compare.
   bcrypt/argon2 rejected: a new dependency for a demo-scale work factor the
   parameter already controls.
3. **Every `/v1` route resolves a Principal from the bearer header** — 401
   before touching the store or the queue. `/healthz` and `/readyz` stay
   open because compose healthchecks and the nginx proxy are not users.
4. **Quota = reservation at submission, settlement at completion.** A
   submission checks `used + 6 ≤ limit` against the tenant's calendar-month
   ledger (6 = the orchestrator's structural call ceiling, DESIGN §11); the
   worker books the exact call count when the graph reaches a terminal state.
   Failed/cancelled runs are not billed — the ledger records finished work,
   so the error direction is conservative under-counting, never double-billing.
5. **Settlement resolves the tenant through the task row**, never from the
   request path: usage follows ownership as it was persisted, and a re-issued
   token cannot move a task's cost between tenants.

## Consequences

- Logout is not a thing (stateless): short TTL is the revocation story. Fine
  for a self-hosted demo; a real deployment adds a denylist or switches to
  asymmetric tokens.
- The reservation number duplicates the graph's bound in the API layer.
  Pinned from both sides: `test_llm_call_count_is_bounded` proves the bound,
  `test_submit_reserves_the_structural_ceiling_against_tenant_quota` proves
  the quote — if the topology changes, one of the two goes red.
- Calendar-month windows use `date_trunc` in SQL: one boundary definition,
  no clock maths split between Python and Postgres.
