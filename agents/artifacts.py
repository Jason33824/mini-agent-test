"""Artifact contracts (v1): the only thing agents exchange. See docs/DESIGN.md section 3."""
from typing import Any, Literal

from pydantic import BaseModel, Field

ArtifactVersion = Literal["v1"]


class BaseArtifact(BaseModel):
    version: ArtifactVersion = "v1"
    task_id: str
    idempotency_key: str
    produced_by: str


class CoverageItem(BaseModel):
    area: str
    risk: str
    covered_by: list[str] = Field(default_factory=list)


class DesignArtifact(BaseArtifact):
    kind: Literal["design"] = "design"
    scope: list[str]
    risks: list[str]
    coverage_matrix: list[CoverageItem]


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
    registry = {"design": DesignArtifact, "cases": CaseArtifact, "report": ExecutionReport}
    if kind not in registry:
        raise ValueError(f"unknown artifact kind: {kind!r}")
    return registry[kind](**payload)
