# ADR 0002: Multi-agent collaboration — supervisor first, topology later

- **Status:** accepted
- **Date:** 2026-10-15
- **Supersedes:** single-pipeline scope of ADR 0001's v0

## Context

v0 was a single 3-node pipeline: one agent, three nodes, one gate. Industry
practice at major tech companies is multi-agent: narrow domain agents
collaborating over structured outputs, humans at gates. In the testing domain
the natural decomposition maps to the workflow stages: test design, whitebox
analysis, test cases, execution. A single agent conflates all of these —
prompts bloat, evaluation blurs, and per-stage cost attribution is impossible.

## Decision

1. Four domain agents (test-design, whitebox, test-case, test-execution) under
   one supervisor orchestrator.
2. **Phase 1 (W1.5-W3):** all agents in one worker process as subgraphs —
   supervisor handoff. Validates the protocol: pydantic artifact contracts,
   gate semantics, bounded revise loops.
3. **Phase 2 (W4+):** agents become independent containers with redis streams
   between them; dead-letter queues, per-agent scaling. Only topology changes;
   the artifact contract does not.
4. Whitebox runs as a parallel evidence producer merged into the design
   artifact — it is a support role, never a blocking stage.
5. Human gates stay in the graph as explicit nodes (two: after design, after
   cases).

## Consequences

- The repo layout changes: `agents/` package with one folder per agent, plus
  `orchestrator.py`; the W1 single pipeline is absorbed as the test-design
  agent's internal graph.
- Phase 1 accepts single-worker throughput limits; queues already exist (ARQ),
  so Phase 2 is a topology change, not a rewrite.
- Evaluation splits into per-agent suites and orchestrator (gate) suites from
  day one.

## Rejected alternatives

- **Distributed from day one:** debugging the protocol and the topology at the
  same time; two complexity sources, one brain.
- **Free-form agent chat:** no testable contract; cost and failures become
  unattributable.
- **Hard-coded pipeline:** no collaboration semantics; renaming it multi-agent
  would be branding, not architecture.
