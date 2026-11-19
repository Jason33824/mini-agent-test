"""Disposition policy unit suite (DESIGN.md section 12): the design-owned,
deterministic answer to whitebox evidence. Pure function — no graph, no LLM."""
import copy
import itertools

from agents.artifacts import DesignArtifact, load_artifact
from agents.test_design.disposition import design_anchors, disposition_for

DESIGN = {
    "scope": ["checkout"],
    "risks": ["double-charge"],
    "coverage_matrix": [{"area": "checkout", "risk": "double-charge", "covered_by": []}],
}
_SEQ = itertools.count()


def _item(source, risk_hint="some risk", finding="a finding"):
    return {
        "id": f"evidence-{next(_SEQ)}",
        "source": source,
        "finding": finding,
        "risk_hint": risk_hint,
    }


def test_anchors_are_scope_union_matrix_areas():
    # the same anchor set the test-case link_check enforces
    assert design_anchors(DESIGN) == {"checkout"}
    wider = {**DESIGN, "coverage_matrix": [{"area": "refund", "risk": "r", "covered_by": []}]}
    assert design_anchors(wider) == {"checkout", "refund"}


def test_evidence_within_scope_is_integrated():
    item = _item("checkout")
    out = disposition_for(DESIGN, [item])
    assert out[0]["evidence_id"] == item["id"]
    assert out[0]["decision"] == "integrate"
    assert out[0]["reason_code"] == "anchor_match"
    assert out[0]["review"] == "not_needed"
    assert "within" in out[0]["reason"] and "checkout" in out[0]["reason"]


def test_evidence_hitting_a_matrix_area_is_integrated():
    design = {**DESIGN, "coverage_matrix": [{"area": "refund", "risk": "r", "covered_by": []}]}
    out = disposition_for(design, [_item("refund")])
    assert out[0]["decision"] == "integrate"


def test_evidence_outside_scope_is_rebutted_with_auditable_reason():
    out = disposition_for(DESIGN, [_item("interface specs", risk_hint="negative refund")])
    assert out[0]["decision"] == "rebut"
    assert out[0]["reason_code"] == "outside_declared_scope"
    assert out[0]["review"] == "pending"  # agent proposal, not a human outcome
    # the reason cites the scope boundary and the asserted risk — auditable,
    # not a black box
    assert "interface specs" in out[0]["reason"]
    assert "outside" in out[0]["reason"]
    assert "checkout" in out[0]["reason"]
    assert "negative refund" in out[0]["reason"]


def test_one_disposition_per_item_in_evidence_order():
    items = [_item("checkout"), _item("interface specs"), _item("checkout")]
    out = disposition_for(DESIGN, items)
    assert [d["source"] for d in out] == ["checkout", "interface specs", "checkout"]
    assert [d["decision"] for d in out] == ["integrate", "rebut", "integrate"]
    assert [d["evidence_id"] for d in out] == [item["id"] for item in items]


def test_same_source_findings_keep_distinct_identities():
    # area/source is the veto key, not the finding identity (SARIF-style)
    items = [
        _item("interface specs", finding="negative amount accepted"),
        _item("interface specs", finding="missing idempotency check"),
    ]
    out = disposition_for(DESIGN, items)
    assert len({d["evidence_id"] for d in out}) == 2
    assert [d["evidence_id"] for d in out] == [item["id"] for item in items]


def test_empty_evidence_yields_no_dispositions():
    assert disposition_for(DESIGN, []) == []


def test_empty_design_anchors_rebut_every_item():
    bare = {"scope": [], "coverage_matrix": []}
    out = disposition_for(bare, [_item("anything")])
    assert out[0]["decision"] == "rebut"
    assert "none" in out[0]["reason"]


def test_policy_is_pure_and_idempotent():
    design = copy.deepcopy(DESIGN)
    items = [_item("checkout"), _item("interface specs")]
    first = disposition_for(design, items)
    second = disposition_for(design, items)
    assert first == second  # deterministic: a revise round recomputes cleanly
    assert design == DESIGN  # never mutates the design it reads


def test_dispositions_validate_through_the_artifact_contract():
    # the policy output is validated at the merge boundary via load_artifact;
    # a merged design carrying dispositions is a legal v1 DesignArtifact
    merged = {
        "version": "v1", "kind": "design", "task_id": "t", "idempotency_key": "t:design",
        "produced_by": "test_design", **DESIGN,
        "dispositions": disposition_for(DESIGN, [_item("checkout"), _item("interface specs")]),
    }
    artifact = load_artifact(merged)
    assert isinstance(artifact, DesignArtifact)
    assert [d.decision for d in artifact.dispositions] == ["integrate", "rebut"]


def test_draft_without_dispositions_defaults_to_empty():
    # the raw design draft (parallel branch) carries no dispositions; the field
    # is optional so the draft validates before the fan-in fills it
    draft = {
        "version": "v1", "kind": "design", "task_id": "t", "idempotency_key": "t:design",
        "produced_by": "test_design", **DESIGN,
    }
    assert load_artifact(draft).dispositions == []