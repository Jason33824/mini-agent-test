"""Artifact contracts (v1): the only thing agents exchange. See docs/DESIGN.md section 3."""
import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

ArtifactVersion = Literal["v1"]


def evidence_fingerprint(task_id: str, item: dict[str, Any], index: int) -> str:
    """Stable per-task identity for one evidence finding.

    Like SARIF's finding fingerprints, identity combines finding content with
    its position in the evidence list, never the display area (`source`) alone.
    Including every item's index — not just a duplicate counter — keeps earlier
    ids stable when a same-source item appears later, while equal-source and
    byte-identical findings still get distinct ids. The agent stamps ids at
    the boundary so crash/retry and revise rounds retain them; the model never
    invents them.
    """
    content = json.dumps(
        [item.get("source", ""), item.get("finding", ""), item.get("risk_hint", "")],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{task_id}:evidence:{content}:{index}"))


class BaseArtifact(BaseModel):
    version: ArtifactVersion = "v1"
    task_id: str
    idempotency_key: str
    produced_by: str


class CoverageItem(BaseModel):
    area: str
    risk: str
    covered_by: list[str] = Field(default_factory=list)
    # set only on rows folded from whitebox evidence; design's own rows have none
    evidence_id: str | None = None


class DispositionItem(BaseModel):
    """The design agent's answer to one whitebox evidence item (DESIGN.md section 12).

    Filled at the merge fan-in by the design-owned disposition policy — not by
    the design draft (the two agents run in parallel, so the draft never sees
    evidence). `evidence_id` is the finding's stable identity; `source` remains
    the coverage-matrix area and the gate veto key. `reason_code` is the
    machine-readable triage reason, while `review` separates the agent's
    proposal from the human outcome: a rebuttal starts `pending`, then is
    `kept` (human override) or `dropped` (rebuttal upheld). Dropped findings
    stay in this list as an audit trail even though their matrix row is gone.
    """

    evidence_id: str = Field(min_length=1)
    source: str
    decision: Literal["integrate", "rebut"]
    reason_code: Literal["anchor_match", "outside_declared_scope"]
    reason: str
    review: Literal["not_needed", "pending", "kept", "dropped"]


class DesignArtifact(BaseArtifact):
    kind: Literal["design"] = "design"
    scope: list[str]
    risks: list[str]
    coverage_matrix: list[CoverageItem]
    # design's response to each merged evidence item; empty on the raw draft,
    # populated by the orchestrator's fan-in (advisory: rebutted rows still
    # merge, flagged at the gate for human arbitration).
    dispositions: list[DispositionItem] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    # Optional for backward compatibility with pre-fingerprint v1 payloads.
    # New whitebox outputs always stamp it; merge backfills it for legacy data.
    id: str = ""
    source: str
    finding: str
    risk_hint: str


class WhiteboxEvidence(BaseArtifact):
    kind: Literal["evidence"] = "evidence"
    items: list[EvidenceItem]


class TestCaseItem(BaseModel):
    title: str
    steps: list[str]
    data: str
    assertion: str
    priority: Literal["P0", "P1", "P2"]
    design_ref: str


class CaseArtifact(BaseArtifact):
    kind: Literal["cases"] = "cases"
    items: list[TestCaseItem]


class CaseResult(BaseModel):
    title: str
    passed: bool
    detail: str


class ExecutionReport(BaseArtifact):
    kind: Literal["report"] = "report"
    results: list[CaseResult]


def load_artifact(payload: dict[str, Any]) -> BaseArtifact:
    """Typed handoff: unknown versions are rejected, never forwarded silently."""
    version = payload.get("version")
    if version != "v1":
        raise ValueError(f"unsupported artifact version: {version!r}")
    kind = payload.get("kind")
    registry = {
        "design": DesignArtifact,
        "evidence": WhiteboxEvidence,
        "cases": CaseArtifact,
        "report": ExecutionReport,
    }
    if kind not in registry:
        raise ValueError(f"unknown artifact kind: {kind!r}")
    return registry[kind](**payload)
