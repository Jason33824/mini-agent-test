# ADR 0005: disposition — a design-owned policy, not a fifth LLM call

- **Status:** accepted
- **Date:** 2026-11-19
- **Extends:** [ADR 0002](0002-multi-agent-collaboration.md) (asymmetric roles, parallel evidence), [ADR 0003](0003-jwt-and-tenant-quotas.md) (reserved cost), DESIGN.md §11 (call bound) and §12 (disagreement resolution)

## Context

Design and whitebox run in parallel and are merged at a fan-in; a
disagreement shows up as two coverage-matrix rows over the same area. The
veto (DESIGN §12) lets a human drop an area, but the human has to find the
contested rows by scanning the full matrix — the gate shows *what* was
merged, not *whether the agents agree*. The missing half of §12 is the design
agent's answer to each evidence item: `integrate` or `rebut`, with a reason,
so the gate can surface only the contested rows.

Two constraints shape where that answer can come from:

1. The design draft cannot see evidence (parallel branches), so the answer
   can only be produced at or after the fan-in.
2. §11 pins the call bound as a topology fact (4 happy-path calls, 6 worst
   case) and the API reserves exactly that ceiling per task (ADR 0003). An
   answer that costs an LLM call changes the bound to 5/8 and moves the
   reservation with it.

## Decision

1. **Disposition is a deterministic, design-owned policy, invoked by the
   orchestrator at the fan-in.** `agents/test_design/disposition.py` answers
   every evidence item against the design's declared anchors — `scope ∪ its
   own matrix areas`, the same set the test-case `link_check` enforces. An
   area the design already stands behind is `integrate`; an area outside the
   declared scope is `rebut` (whitebox asserts a risk the design did not plan
   for; the design defends its scope boundary). Each answer carries a reason
   that cites the anchors and the asserted risk, so the verdict is auditable
   rather than a bare flag.
2. **Ownership stays asymmetric.** The policy lives in the design agent's
   package (it is the design's judgement) but runs in the orchestrator's
   merge node (the only point where both artifacts coexist). The design draft
   never emits dispositions — any the model hallucinates are stripped, so the
   policy is the single source. Whitebox remains unable to touch the design.
3. **A rebuttal is advisory, never a silent drop.** Rebutted rows still merge
   into the matrix; the gate marks them as agent disagreement. Only a human
   removes evidence, which keeps the §12 invariant that evidence is never
   overwritten by the other agent.
4. **Arbitration reuses the veto.** Upholding a rebuttal = `drop_areas` on
   the existing gate decision; approving without one overrides the rebuttal.
   No new route, event, or decision field. The veto is now reflected into the
   design the gate releases downstream, so it holds on the *approve* path
   (forward to cases without a re-merge) exactly as it holds on the reject
   path (re-run design, merge skips dropped areas) — the two paths are
   idempotent with respect to each other.
5. **Dispositions ride the artifact, not a side channel.** They are an
   optional field on `DesignArtifact` (v1 stays v1: a draft without the field
   validates, the merged composite carries it). The `gate.request` payload,
   the persisted artifact row, and the artifact cards all see them for free.
6. **Triage is identified, reasoned, and reviewable — the SARIF shape.**
   Whitebox stamps a deterministic `evidence_id` from task + finding content
   + list position; merged rows and dispositions share that id, so `source`
   (the area) is never mistaken for finding identity. Loading pre-fingerprint
   v1 evidence remains valid and merge backfills the id, so an upgrade does
   not strand an old checkpoint. Each answer also has a `reason_code` and a `review` lifecycle: `pending` → `kept` (override) or
   `dropped` (uphold). A veto removes the matrix row but retains the dropped
   disposition and its justification for audit, following the same principle
   as [SARIF suppressions](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html)
   and [code-scanning dismissal records](https://docs.github.com/en/code-security/how-tos/manage-security-alerts/manage-code-scanning-alerts/resolve-alerts).

## Rejected alternatives

- **An LLM "critique" call at the fan-in.** Richer verdicts (it could weigh
  the finding, not just the area), but it adds one call per merge pass — the
  happy path becomes 5 and the worst case 8, the reservation in ADR 0003 has
  to move, and both pinning tests go red by design. The anchor rule already
  captures the one disagreement that is structural (whitebox found a risk
  the design did not scope); judging the *content* of a finding is the
  human's job at the gate, which is why the gate exists. Revisit only if
  real traffic shows out-of-scope evidence is mostly noise rather than signal
  — then the cost of a call buys triage.
- **Rebut = drop the row before the gate.** Lets the agents "settle" among
  themselves, which is exactly the destructive disagreement §12 rules out:
  the human would never see what whitebox found.
- **Delete a disposition once it is vetoed.** Cheaper to render, but it
  destroys the finding's dismissal history and makes a later revise look as
  if the evidence never existed. The row goes; the identified review record
  stays.
- **A separate `dispositions` event or gate field.** Another contract to
  version and another place the frontend has to join; the artifact already
  flows through every surface the gate needs.

## Consequences

- The §11 bound and the ADR 0003 reservation are untouched; a regression
  test pins that dispositions are present *and* the design agent made
  exactly one call.
- The gate UI shows pending contested areas to arbitrate (rebuttals grouped
  by area, reasons keyed by finding id), previously arbitrated findings, or
  agent agreement (all integrated). The full-matrix veto list remains as the
  fallback for an artifact without dispositions. The artifact card shows the
  final `kept` / `dropped` review outcome alongside the original reason.
- The policy is intentionally coarse (anchor membership, exact string match,
  consistent with `link_check`). A design that lists an area in its scope
  integrates any evidence about it, even a finding it would disagree with —
  that residual disagreement is visible in the matrix (two rows, same area)
  and still goes to the human; the policy narrows the human's attention, it
  does not replace it.
