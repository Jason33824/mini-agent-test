"""Shared state for the PRD -> test-cases pipeline."""
from typing import TypedDict


class PipelineState(TypedDict):
    prd: str
    draft: str
    review_feedback: str
    approved: bool
    final: str
