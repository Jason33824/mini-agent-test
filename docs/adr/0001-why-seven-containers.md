# ADR 0001: Why an independent repo with seven containers

- **Status:** accepted
- **Date:** 2026-10-15

## Context

The problem source is an internal AI testing platform (agent as Project Test
Manager, 80+ daily users). It runs as a locally-distributed tool by design:
it must read requirement docs and business code that cannot leave the
network, so centralizing it as a hosted service was never on the table.
That leaves the production elements (async queues, auth, quotas, centralized
observability) as the real gap — and those cannot be learned by patching a
tool that 80 people use every day.

## Decision

1. Build a **separate, from-scratch repo** — not a refactor of the internal
   platform. Learning, open-source artifact, and interview evidence need
   different constraints than a live internal tool.
2. Seven containers: web / api / worker / redis / postgres / phoenix /
   phoenix-pg. Every production element maps to a concrete, observable
   interaction with one of them (see README table).
3. Replace Chainlit with a minimal hand-rolled frontend: Chainlit hides
   login state, SSE, and session management — exactly the things this
   project exists to expose.
4. PostgreSQL (shared instance for app data; separate for Phoenix metadata).

## Consequences

- The graph logic (generate -> review gate -> refine) is reimplemented
  rather than reused; acceptable, it is 3 nodes.
- Frontend work is non-trivial but scoped to one page.
- Container count is the floor, not a goal: no service exists without a
  production element that justifies it.
