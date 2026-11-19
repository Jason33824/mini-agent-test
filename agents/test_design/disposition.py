"""Design-owned disposition policy (DESIGN.md section 12).

The design agent answers every whitebox evidence item with `integrate` or
`rebut` plus a reason. The answer is a deterministic policy over the design's
own declared coverage anchors — not an LLM call — so the structural call bound
(DESIGN.md section 11: 4 happy-path calls, 6 worst case) is untouched.

Why here and not in the design draft: design and whitebox fan out in parallel,
so the draft never sees evidence. The orchestrator invokes this policy at the
merge fan-in, the only point where the two artifacts coexist, against a
deep-copy of the design (the orchestrator never mutates an upstream artifact).

Semantics — the roles are asymmetric (whitebox emits facts, design emits
decisions):
  * integrate: the evidence area hits a declared anchor, so the fact reinforces
    a decision the design already made. No disagreement.
  * rebut: the evidence area is outside the design's declared scope. Whitebox
    asserts a risk the design did not plan for; design defends its scope
    boundary. This is an agent disagreement, surfaced at the gate so the human
    arbitrates only the contested row (uphold = veto the area, override = keep).

Each answer links to the finding by its stable `evidence_id` and carries a
machine-readable `reason_code`. A rebut is advisory: its row still merges
(evidence is never silently dropped) and starts with `review="pending"`; the
human gate later records `kept` (override) or `dropped` (upheld). Dropped
findings remain in the disposition list for audit even after their matrix row
is removed. Absent evidence yields no dispositions (degrade never blocks).
"""
from typing import Any


def design_anchors(design: dict[str, Any]) -> set[str]:
    """The design's declared coverage anchors: scope ∪ its own matrix areas.

    Identical to the anchor set the test-case link_check enforces, so a case
    may link to any area the design stands behind — including integrated
    evidence rows once they are merged.
    """
    return set(design.get("scope", [])) | {
        row.get("area", "") for row in design.get("coverage_matrix", [])
    }


def disposition_for(design: dict[str, Any], evidence_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One disposition per evidence item, in evidence order. Pure and idempotent:
    the same design + evidence always yields the same answer, so a revise round
    recomputes cleanly instead of accumulating."""
    anchors = design_anchors(design)
    answered: list[dict[str, Any]] = []
    for item in evidence_items:
        source = item.get("source", "")
        if source in anchors:
            answered.append(
                {
                    "evidence_id": item.get("id", ""),
                    "source": source,
                    "decision": "integrate",
                    "reason_code": "anchor_match",
                    "reason": (
                        f"area '{source}' is within the design's declared coverage anchors;"
                        " the evidence reinforces planned coverage."
                    ),
                    "review": "not_needed",
                }
            )
        else:
            answered.append(
                {
                    "evidence_id": item.get("id", ""),
                    "source": source,
                    "decision": "rebut",
                    "reason_code": "outside_declared_scope",
                    "reason": (
                        f"area '{source}' is outside the design's declared scope"
                        f" ({sorted(anchors) or 'none'}); whitebox asserts risk"
                        f" '{item.get('risk_hint', '')}' — contested, for human arbitration."
                    ),
                    "review": "pending",
                }
            )
    return answered