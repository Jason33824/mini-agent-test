# ADR 0004: industry API conventions — borrowed, not invented

- **Status:** accepted
- **Date:** 2026-11-03
- **Extends:** ADR 0003 (JWT auth + tenant quotas), DESIGN.md §11 (what stays out of the graph)

## Context

A production agent platform talks to flaky providers and unreliable networks.
The patterns for surviving that are not novel — Stripe, OpenAI, and the IETF
draft rate-limit fields have settled the API surface conventions, the OpenAI
SDK settled the client retry policy, and OpenInference settled how LLM spans
should look to a trace UI. Inventing local variants would be the tell of a
toy: everyone reviewing this repo already knows the real ones.

## Decision

1. **`Idempotency-Key` on submission (Stripe pattern).** A retried POST with a
   seen key replays the original task — no second row, no second enqueue — and
   answers with an `idempotent-replayed: true` header. Keys are scoped per
   user via a partial unique index (`WHERE idempotency_key IS NOT NULL`), so
   key reuse across users is legal and keyless submissions never collide.
2. **Provider retry policy = OpenAI SDK defaults.** 408/409/429/5xx and
   transport errors are retried (`MINI_AGENT_LLM_MAX_RETRIES`, default 2);
   exponential backoff from 0.5s capped at 8s with full jitter; a server
   `Retry-After` wins over the computed delay, itself capped at 60s so a
   "come back in an hour" never parks a semaphore slot. Anything else (400,
   401, 422) fails fast — retrying a malformed prompt never helps.
3. **OpenAI-style rate headers on submission.** Every submit response carries
   `x-ratelimit-limit` / `-remaining` / `-reset` (remaining is after the
   worst-case reservation; reset is the unix ts of the next calendar month);
   the 429 adds `retry-after` in delta-seconds per RFC 9110.
4. **OpenInference span kinds.** `openinference.span.kind` on every span —
   CHAIN for task roots and agent handoffs, LLM for provider calls — so the
   Phoenix UI renders token counters and per-agent views natively. The
   attribute key is a plain string: no new dependency for a convention.
5. **Retries are invisible to the trace tree.** One logical LLM call is one
   `llm.chat` span; attempts land on it as `llm.retry_count` rather than
   spawning sibling spans (quota settlement counts calls, not attempts).

## Rejected alternatives

- **LiteLLM-style per-call budget enforcement in the gateway.** Their proxy
  re-checks spend before every request because many untrusted callers share
  one gateway. Here the budget is enforced at ingress with reservation
  semantics (ADR 0003) — the worst case is pinned *before* the graph runs, so
  a second accounting path would have to live inside the graph, which §11
  rules out. One enforcement point, one ledger.
- **Full Stripe response replay** (cached body + status verbatim per key).
  Demo scale: the replay returns the task's *current* snapshot instead — the
  invariant that matters (no dupe task, no dupe enqueue, marked as replay)
  holds either way.

## Consequences

- Worst-case slot hold during retries: two backoffs (<=16s total) or one
  `Retry-After` (<=60s) inside the semaphore — bounded and deliberate:
  releasing the slot mid-retry would re-queue work the provider already
  agreed to serve soon.
- The replay header is lowercase `idempotent-replayed`, not Stripe's
  `Idempotent-Replayed`: HTTP/2 field names are case-insensitive, and the
  FastAPI/uvicorn stack normalizes to lowercase everywhere.
