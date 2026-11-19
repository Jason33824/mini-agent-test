# Multi-agent design

Detailed design for agent collaboration in mini-agent-test. Decision
rationale lives in [ADR 0002](adr/0002-multi-agent-collaboration.md); this
document is the how.

## 1. Principles

1. **Protocol before topology.** First make the collaboration correct
   (artifact schemas, gate semantics), then make it distributed (independent
   containers, queues). Never introduce both complexities at once.
2. **Agents exchange artifacts, never prose.** Every handoff is a versioned,
   pydantic-validated payload. If an agent needs to "just explain", that
   explanation belongs in a schema field.
3. **Human gates are first-class citizens.** A gate is a graph node with its
   own state, not an afterthought bolted onto a chat loop.
4. **Every token is attributable.** Cost is aggregated per agent per task; the
   first question an operator asks is "which agent burns money", so the trace
   tree must answer it by construction.

## 2. Agents and responsibilities

| Agent | Input | Output artifact | Responsibility | Failure semantics |
|---|---|---|---|---|
| **orchestrator** (supervisor) | user task | `ExecutionReport` (final) | Route task -> agents; insert human gates; aggregate report | Retries handoffs; never edits an agent's own draft — the merged design is an orchestrator-built composite (fan-in + human veto) |
| **test-design** | `TaskInput` (PRD text) | `DesignArtifact` | What to test: strategy, risks, coverage matrix | Bounded revise loop on gate rejection |
| **whitebox** | PRD + interface specs | `WhiteboxEvidence` | Static view: interface contracts, boundary values, branch points; runs in parallel with design and merges as evidence | Optional stage; missing evidence degrades design, never blocks |
| **test-case** | `DesignArtifact` (+ evidence) | `CaseArtifact` | Concrete cases: steps, data, assertions, priority | Bounded revise loop; cases must link to design items |
| **test-execution** | approved `CaseArtifact` | `ExecutionReport` | Orchestrate runs, collect results per case | Per-case retry; report marks failed cases explicitly |

## 3. Artifact contract (v1)

```python
class BaseArtifact(BaseModel):
    version: Literal["v1"]
    task_id: str
    idempotency_key: str          # uuid5(task_id, agent, stage)
    produced_by: AgentName

class DesignArtifact(BaseArtifact):
    kind: Literal["design"]
    scope: list[str]              # what to test
    risks: list[str]
    coverage_matrix: list[CoverageItem]
    dispositions: list[DispositionItem]   # design's answer per evidence item (section 12)

class DispositionItem(BaseModel):
    evidence_id: str              # stable finding identity (= evidence row link)
    source: str                   # the evidence item's source (= its matrix area / veto key)
    decision: Literal["integrate", "rebut"]
    reason_code: Literal["anchor_match", "outside_declared_scope"]
    reason: str
    review: Literal["not_needed", "pending", "kept", "dropped"]

class WhiteboxEvidence(BaseArtifact):
    kind: Literal["evidence"]
    items: list[EvidenceItem]     # id / source / finding / risk_hint

class CaseArtifact(BaseArtifact):
    kind: "cases"
    items: list[TestCase]         # steps / data / assertion / priority / design_ref
```

- **Versioning:** agents validate `version` and reject unknown values with a
  typed error — a schema change is a failing test, not silent drift.
- **Idempotency key:** worker restarts re-emit the same artifact id, so retry
  after a crash cannot duplicate downstream effects.
- **Dispositions are a fan-in field.** The raw design draft leaves
  `dispositions` empty (the parallel branch cannot see evidence); the
  orchestrator's `merge_evidence` node fills it on the merged composite via
  the design-owned policy (section 12). The design artifact a gate reviews
  and the worker persists is that composite — evidence rows, dispositions,
  and any human veto — not the design agent's own draft.
- **Finding identity is not the area name.** Whitebox stamps every item with
  a deterministic `evidence_fingerprint(task_id, item, index)` at its agent
  boundary. Merged coverage rows and dispositions carry that same
  `evidence_id`, so same-source findings stay distinct; `source` remains the
  area-level veto key. The id is optional when loading older v1 evidence, and
  merge backfills it with the same rule, so pre-fingerprint checkpoints remain
  resumable. A rebuttal starts `review=pending`; the gate records `kept`
  (override) or `dropped` (upheld), and dropped dispositions remain as an
  audit trail after their matrix rows are removed.

## 4. Collaboration sequence

### Topology — the orchestrator graph

```mermaid
flowchart TD
    S((task in)) --> D[test-design agent<br/>draft - validate]
    S --> B[whitebox agent<br/>analyze - validate]
    D --> M["merge_evidence<br/>fold evidence + disposition policy<br/>deepcopy: never mutate upstream"]
    B --> M
    M --> G1{"GATE 1: design<br/>interrupt, resume with decision"}
    G1 -- approved --> C[test-case agent<br/>draft - link_check - validate]
    G1 -- "rejected: revise loop" --> D
    G1 -- "rejected: exhausted" --> S1["surface_design<br/>outcome = rejected_design"]
    C --> G2{"GATE 2: cases<br/>interrupt, resume with decision"}
    G2 -- approved --> E[test-execution agent<br/>run - validate]
    G2 -- "rejected: revise loop" --> C
    G2 -- "rejected: exhausted" --> S2["surface_cases<br/>outcome = rejected_cases"]
    E --> F[finish<br/>outcome = report]
```

Revise rounds re-run **only the design branch** (whitebox evidence is static
fact and survives; the merged matrix is rebuilt from the fresh design on
every pass — the deepcopy rule keeps this idempotent).

### Message sequence

```mermaid
sequenceDiagram
    participant U as User
    participant O as Orchestrator
    participant D as test-design
    participant B as whitebox
    participant C as test-case
    participant E as test-execution

    U->>O: submit task (PRD)
    par design with parallel evidence
        O->>D: TaskInput
        O->>B: TaskInput (specs)
        B-->>O: WhiteboxEvidence
    end
    D-->>O: DesignArtifact (merges evidence)
    O->>U: HUMAN GATE 1 — approve strategy?
    U->>O: approved / rejected (feedback)
    O->>C: DesignArtifact
    C-->>O: CaseArtifact
    O->>U: HUMAN GATE 2 — approve cases?
    U->>O: approved
    O->>E: CaseArtifact
    E-->>O: ExecutionReport
    O->>U: report
```

## 5. Collaboration mode: two phases

**Phase 1: supervisor in one process.** The orchestrator is a
LangGraph; each agent is a subgraph invoked by handoff. All agents live in one
worker container. This validates the protocol — schemas, gates, revise loops —
with zero distributed-systems overhead.

**Phase 2: agents as independent containers.** Each agent gets its own
worker + queue; the orchestrator publishes handoff messages on redis streams.
Adds: dead-letter queues, per-agent autoscaling, cross-container trace
continuity. Only the topology changes — the artifact contract is untouched,
which is exactly why it was designed first.

Rejected for v0: free-form multi-agent chat (no testable contract), and a
hard-coded pipeline (no collaboration semantics, just a pipe).

## 6. Observability and cost

- The orchestrator opens the root span (`task_id`); every agent handoff opens
  a child span named by agent — one trace tree per task across all agents.
- Token usage is recorded per agent per node; Phoenix attributes roll up to
  per-agent cost. Dashboards answer "which agent burns money" without log
  archaeology.
- Human gates are spans too: gate wait time is a first-class metric (it is
  usually the largest latency component, and hiding it hides the truth).

## 7. Failure semantics

| Failure | Behavior |
|---|---|
| Agent LLM call fails | Node-level retry (bounded); artifact idempotency key prevents duplicates |
| Worker crash mid-graph | ARQ redelivers; graph resumes from last checkpointed state |
| Gate rejected | Normal path, not an error: bounded revise loop (max N), then surface to human |
| Schema mismatch at handoff | Typed rejection, task marked failed — never forwarded silently |
| Cancel requested | Cooperative flag checked at every handoff boundary; current node finishes, next never starts |

## 8. Evaluation

- **Per-agent unit tests:** fixture input -> artifact assertions (schema +
  semantic invariants, e.g. every case links to a design item).
- **Orchestrator tests:** gate semantics (rejection terminates, approval
  proceeds), artifact version rejection.
- **Golden-set end-to-end:** a small frozen PRD set with expected
  artifact shapes; runs in CI as the collaboration regression suite.

## 9. Agent internals

Each domain agent is a small graph of typed nodes. No agent contains a human
gate — gates belong to the orchestrator (section 2), which keeps agents pure
business logic and makes collaboration semantics testable in one place.

| Agent | Nodes | LLM calls | Contract enforcement |
|---|---|---|---|
| test-design | `draft_design → validate` | 1 (PRD + revise feedback) | `load_artifact` after draft |
| whitebox | `analyze → validate` | 1 (PRD/specs) | `load_artifact` after analyze |
| test-case | `draft_cases → link_check → validate` | 1 (design summary) | every `design_ref` must hit an anchor (scope ∪ matrix areas) |
| test-execution | `run_cases → validate` | 1 (cases) | `load_artifact` after run |

The agents share one narrow `AgentState` **I/O envelope**
(`task_id / input_artifact / output_artifact / feedback`), not a universal
business state. Their graphs and versioned artifact contracts stay separate:
whitebox produces evidence, design produces scope and a matrix, cases adds a
`link_check` node, execution produces a report. This is the LangGraph
shared-schema subgraph boundary — reuse the transport shape, do not merge
`scope / items / results` into one bag and do not collapse the agents into a
single configurable node. The small structural similarity (draft → validate)
is not enough reason for a framework abstraction.

Evidence merge lives in the **orchestrator** (`merge_evidence` node), not in
the design agent: the two agents run in parallel, so the design agent cannot
see whitebox output. Two invariants make the merge safe:

- **Deep-copy on join.** The orchestrator never mutates an upstream artifact.
  A shallow copy shares the nested `coverage_matrix` list, and a revise round
  would fold the same evidence twice — silently breaking handoff idempotency.
- **Degrade, never block.** Whitebox failure marks evidence empty and logs a
  warning; the design proceeds on its own matrix (section 2 semantics).

The fan-in also runs the design agent's **disposition policy**
(`agents/test_design/disposition.py`, section 12): the design answers each
evidence item against its own declared anchors before the evidence folds in.
The policy is design-owned (it lives in the design agent's package) but
orchestrator-invoked, because the fan-in is the only point where design and
evidence coexist. It is a deterministic function, not an LLM call, so the
section 11 call bound is unchanged.

LLM access is a single injectable seam (`agents/llm.py`): production uses an
OpenAI-compatible endpoint from env config; tests inject `FakeLLM`, whose
responses are routed by system-prompt substring (parallel branches consume
independent queues, so fan-out order never matters) and deep-copied on
consume.

### Inside the agents

```mermaid
flowchart LR
    subgraph SD[test-design]
        d1[draft_design<br/>LLM] --> d2[validate<br/>load_artifact]
    end
    subgraph WB[whitebox]
        w1[analyze<br/>LLM] --> w2[validate<br/>load_artifact]
    end
    subgraph TC[test-case]
        c1[draft_cases<br/>LLM] --> c2[link_check<br/>design_ref hits anchors] --> c3[validate<br/>load_artifact]
    end
    subgraph TE[test-execution]
        e1[run_cases<br/>LLM] --> e2[validate<br/>load_artifact]
    end
```

## 10. Revise loop

Human gates are implemented with `langgraph.types.interrupt` + a checkpointer:
the gate node suspends the graph, the API resumes it with
`{"approved": bool, "feedback": str}`.

- **approved** → next stage.
- **rejected, attempts < MAX_REVISE (=2)** → back to the stage's handoff; the
  agent re-runs with the feedback injected into its prompt.
- **rejected, attempts exhausted** → `surface_<stage>` node records
  `outcome="rejected_<stage>"` and ends the run — a human decides what is
  next; the loop never spins silently.

Whitebox evidence survives revise rounds: a rejected design re-runs only the
design branch, and static facts are reused. The fan-in node clears consumed
feedback, which also keeps parallel branches free of shared state writes.

## 11. Token & cost model

**Call-count bound (structural).** The happy path makes exactly **4 LLM calls
per task** — one per agent; each revise round adds exactly 1. With
MAX_REVISE=2 the per-task upper bound is **6 calls**. The bound is a property
of the graph topology, not an aspiration — it is pinned by a regression test
(`test_llm_call_count_is_bounded`). Collaboration mechanics that are not
agent work — evidence merge, disposition (section 12), gate arbitration —
are deterministic orchestrator or policy code and spend no calls, which is
what keeps the bound a topology fact.

**Context shape.** design and whitebox each consume the full PRD (parallel
branches: unavoidable 2×PRD); cases consumes the design artifact; execution
consumes the cases artifact — context grows linearly downstream. If real
artifacts grow large, the lever is summary-forwarding (pass digests between
agents, fetch full artifacts by id); not needed at v0 sizes.

**Model routing.** Each agent resolves its model through env: a
per-agent override (`MINI_AGENT_LLM_MODEL_<AGENT>`) falls back to the shared
`MINI_AGENT_LLM_MODEL`. Typical split: strong model for design/case
generation, cheaper model for execution verdicts. Per-agent token attribution
(section 6) shows where the money goes; routing is how you act on it.

**Not in the graph — the quota layer (ADR 0003).** Per-tenant budget
enforcement lives beside the state machine, not inside it: the API *reserves*
the structural ceiling (6 calls) against the tenant's monthly ledger at
submission — over-cap requests get 429 before touching the queue — and the
worker *settles* the exact call count when the graph reaches a terminal
state. The graph stays a pure workflow and does not duplicate platform
concerns. Provider-side prompt caching is the lever once real traffic shows
repeat prefixes.

## 12. Disagreement resolution

Design and whitebox **cannot disagree destructively**, by construction: the
roles are asymmetric. Whitebox emits factual evidence (`source / finding /
risk_hint`) and has no write access to the design; design emits decisions and
has no write access to evidence. Merge folds each evidence item into its own
coverage-matrix row next to the design's own judgement — disagreement shows
up as two rows over the same area, never as one overwriting the other.

The arbitration owner is the **human gate**, with two mechanisms:

- **Disposition.** The design agent answers every evidence item with
  `integrate` or `rebut` plus a reason (`DesignArtifact.dispositions`,
  section 3), computed by the orchestrator's `merge_evidence` fan-in. The
  answer is a deterministic, design-owned policy over the design's declared
  coverage anchors (`scope ∪ matrix areas` — the same set the test-case
  `link_check` enforces): an area the design already stands behind is
  *integrated*; an area outside its declared scope is *rebutted* — whitebox
  asserts a risk the design did not plan for, and the design defends its
  scope boundary. A rebuttal is advisory: the row still merges (evidence is
  never silently dropped), and the gate flags it as agent disagreement so
  the human arbitrates only the contested rows instead of scanning the full
  matrix. Each answer links the finding by `evidence_id` and carries a
  machine-readable `reason_code`; its `review` starts `pending` and becomes
  `kept` (human override) or `dropped` (rebuttal upheld). The policy is a
  pure function: a revise round recomputes dispositions from the fresh
  design, never accumulates them, and it costs no LLM call (section 11).
- **Veto.** A gate decision may carry `drop_areas: [...]` — upholding a
  rebuttal is a veto of that area; approving without one overrides the
  rebuttal and keeps the row. The veto is durable task state: successive gate
  decisions accumulate vetoed areas rather than replacing them, the
  design-gate node applies them to the merged composite released downstream
  (so they hold on the approve path without a revise), and every later merge
  pass skips them. No matrix row is ever re-proposed for an area a human
  has declined; its disposition remains as a `dropped` audit record with the
  original reason and stable finding id (regression-tested).

Failure of whitebox is a degenerate case of the same rule: absent evidence
cannot conflict with anything (section 9, degrade never blocks) — the
disposition list is simply empty.
