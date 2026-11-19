"""Orchestrator suites: collaboration sequence, gates, bounded revise loop, graceful degrade."""
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agents.artifacts import evidence_fingerprint
from agents.llm import FakeLLM
from agents.registry import AGENT_REGISTRY
from orchestrator import MAX_REVISE, _merge_evidence, build_orchestrator

DESIGN_DRAFT = {
    "scope": ["checkout"],
    "risks": ["double-charge"],
    "coverage_matrix": [{"area": "checkout", "risk": "double-charge", "covered_by": []}],
}
EVIDENCE = {
    "items": [
        {"source": "interface specs", "finding": "negative amounts accepted", "risk_hint": "negative refund"}
    ]
}
CASES = {
    "items": [
        {
            "title": "reject negative refund",
            "steps": ["submit refund with amount=-1"],
            "data": "amount=-1",
            "assertion": "400 returned",
            "priority": "P0",
            "design_ref": "checkout",
        }
    ]
}
REPORT = {"results": [{"title": "reject negative refund", "passed": True, "detail": "400 as expected"}]}


def _wire(design_scripted=1):
    AGENT_REGISTRY["test_design"].llm = FakeLLM(
        {"test-design agent": [dict(DESIGN_DRAFT) for _ in range(design_scripted)]}
    )
    AGENT_REGISTRY["whitebox"].llm = FakeLLM({"whitebox analysis agent": [dict(EVIDENCE)]})
    AGENT_REGISTRY["test_case"].llm = FakeLLM({"test-case agent": [dict(CASES)]})
    AGENT_REGISTRY["test_execution"].llm = FakeLLM({"test-execution agent": [dict(REPORT)]})


@pytest.fixture(autouse=True)
def _reset_llm_injection():
    yield
    for agent in AGENT_REGISTRY.values():
        agent.llm = None


def _graph():
    return build_orchestrator(checkpointer=InMemorySaver())


CFG = {"configurable": {"thread_id": "t1"}}


def _resume(graph, decision):
    return graph.invoke(Command(resume=decision), CFG)


def test_full_run_with_approvals():
    _wire()
    graph = _graph()
    out = graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    assert out["__interrupt__"]  # suspended at design_gate
    out = _resume(graph, {"approved": True, "feedback": ""})
    assert out["__interrupt__"]  # then cases_gate
    final = _resume(graph, {"approved": True, "feedback": ""})
    assert final["outcome"] == "report"
    assert final["report"]["results"][0]["passed"] is True
    # evidence merged: design row + whitebox row
    areas = {c["area"] for c in final["design"]["coverage_matrix"]}
    assert areas == {"checkout", "interface specs"}


def test_rejection_triggers_bounded_revise_then_approval():
    _wire(design_scripted=2)
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    out = _resume(graph, {"approved": False, "feedback": "tighten scope"})
    assert out["__interrupt__"]  # design re-ran with feedback, gate again
    out = _resume(graph, {"approved": True, "feedback": ""})  # revised design approved -> cases_gate
    assert out["__interrupt__"]
    final = _resume(graph, {"approved": True, "feedback": ""})
    assert final["outcome"] == "report"
    assert final["design_attempts"] == 2


def test_repeated_rejection_surfaces_to_human():
    _wire(design_scripted=MAX_REVISE)
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    graph.invoke(Command(resume={"approved": False, "feedback": "no"}), CFG)
    final = graph.invoke(Command(resume={"approved": False, "feedback": "still no"}), CFG)
    assert final["outcome"] == "rejected_design"
    assert "__interrupt__" not in final


def test_gate_decision_can_veto_evidence_areas():
    """Human veto on evidence is durable: the dropped area never re-enters the matrix."""
    _wire(design_scripted=2)
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    out = _resume(graph, {"approved": False, "feedback": "skip the static finding", "drop_areas": ["interface specs"]})
    assert out["__interrupt__"]  # revise round
    out = _resume(graph, {"approved": True, "feedback": ""})
    assert out["__interrupt__"]
    final = _resume(graph, {"approved": True, "feedback": ""})
    assert final["outcome"] == "report"
    areas = {c["area"] for c in final["design"]["coverage_matrix"]}
    assert areas == {"checkout"}  # vetoed area absent after a full revise round


def test_llm_call_count_is_bounded():
    """Happy path = exactly 4 LLM calls; the 6-call ceiling is structural (DESIGN.md section 11)."""
    _wire()
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    _resume(graph, {"approved": True, "feedback": ""})
    final = _resume(graph, {"approved": True, "feedback": ""})
    assert final["outcome"] == "report"
    assert len(AGENT_REGISTRY["test_design"].llm.calls) == 1
    assert len(AGENT_REGISTRY["whitebox"].llm.calls) == 1
    assert len(AGENT_REGISTRY["test_case"].llm.calls) == 1
    assert len(AGENT_REGISTRY["test_execution"].llm.calls) == 1


def test_whitebox_failure_degrades_never_blocks():
    _wire()
    AGENT_REGISTRY["whitebox"].llm = FakeLLM({})  # no scripted response -> raises
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    _resume(graph, {"approved": True, "feedback": ""})
    final = _resume(graph, {"approved": True, "feedback": ""})
    assert final["outcome"] == "report"
    areas = {c["area"] for c in final["design"]["coverage_matrix"]}
    assert areas == {"checkout"}  # design's own row only


def test_merge_records_a_disposition_for_every_evidence_item():
    """The rebuttal starts pending; human approval without a veto overrides it
    (`kept`) and the merged matrix row remains linked by the stable finding id."""
    _wire()
    graph = _graph()
    first = graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    pending = first["design"]["dispositions"][0]
    assert pending["decision"] == "rebut" and pending["review"] == "pending"
    _resume(graph, {"approved": True, "feedback": ""})
    final = _resume(graph, {"approved": True, "feedback": ""})
    dispositions = final["design"]["dispositions"]
    assert [d["source"] for d in dispositions] == ["interface specs"]
    assert dispositions[0]["decision"] == "rebut"
    assert dispositions[0]["review"] == "kept"
    assert dispositions[0]["reason_code"] == "outside_declared_scope"
    assert dispositions[0]["reason"]  # auditable reason, not a bare flag
    # advisory: the contested row is still in the matrix for the human to see
    areas = {c["area"] for c in final["design"]["coverage_matrix"]}
    assert areas == {"checkout", "interface specs"}
    evidence_row = next(c for c in final["design"]["coverage_matrix"] if c["area"] == "interface specs")
    assert evidence_row["evidence_id"] == dispositions[0]["evidence_id"] == pending["evidence_id"]


def test_in_scope_evidence_is_integrated():
    _wire()
    AGENT_REGISTRY["whitebox"].llm = FakeLLM(
        {"whitebox analysis agent": [{"items": [
            {"source": "checkout", "finding": "amount field unbounded", "risk_hint": "overflow"}
        ]}]}
    )
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    _resume(graph, {"approved": True, "feedback": ""})
    final = _resume(graph, {"approved": True, "feedback": ""})
    assert final["design"]["dispositions"][0]["decision"] == "integrate"
    assert final["design"]["dispositions"][0]["review"] == "not_needed"


def test_same_source_findings_have_distinct_dispositions_and_matrix_links():
    _wire()
    AGENT_REGISTRY["whitebox"].llm = FakeLLM(
        {"whitebox analysis agent": [{"items": [
            {"source": "interface specs", "finding": "negative amount", "risk_hint": "refund"},
            {"source": "interface specs", "finding": "missing idempotency", "risk_hint": "replay"},
        ]}]}
    )
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    _resume(graph, {"approved": True, "feedback": ""})
    final = _resume(graph, {"approved": True, "feedback": ""})
    dispositions = final["design"]["dispositions"]
    rows = [c for c in final["design"]["coverage_matrix"] if c["area"] == "interface specs"]
    assert len(dispositions) == len(rows) == 2
    assert len({d["evidence_id"] for d in dispositions}) == 2
    assert {row["evidence_id"] for row in rows} == {d["evidence_id"] for d in dispositions}


def test_merge_backfills_fingerprint_for_legacy_v1_evidence():
    """A pre-fingerprint v1 checkpoint can resume after upgrade: merge assigns
    the same deterministic id without mutating the upstream evidence."""
    design = {
        "version": "v1", "kind": "design", "task_id": "t1",
        "idempotency_key": "t1:design", "produced_by": "test_design", **DESIGN_DRAFT,
    }
    item = {"source": "interface specs", "finding": "negative amounts", "risk_hint": "negative refund"}
    legacy = {"items": [item]}
    merged = _merge_evidence({"task_id": "t1", "design": design, "evidence": legacy})
    expected = evidence_fingerprint("t1", item, 0)
    assert merged["design"]["dispositions"][0]["evidence_id"] == expected
    evidence_row = next(c for c in merged["design"]["coverage_matrix"] if c["area"] == "interface specs")
    assert evidence_row["evidence_id"] == expected
    assert "id" not in item  # upstream evidence was not mutated


def test_disposition_recomputed_on_revise_without_accumulation():
    """A revise round re-runs design and recomputes dispositions from the fresh
    draft — the deepcopy rule keeps this idempotent (no duplicate answers)."""
    _wire(design_scripted=2)
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    _resume(graph, {"approved": False, "feedback": "tighten scope"})
    _resume(graph, {"approved": True, "feedback": ""})
    final = _resume(graph, {"approved": True, "feedback": ""})
    assert final["design_attempts"] == 2
    assert len(final["design"]["dispositions"]) == 1  # one evidence item, one answer
    assert final["design"]["dispositions"][0]["source"] == "interface specs"
    assert final["design"]["dispositions"][0]["review"] == "kept"


def test_vetoed_area_drops_row_but_retains_disposition_audit():
    """Upholding a rebuttal is durable: the area never re-enters the matrix,
    while its disposition remains as a `dropped` audit record after revise."""
    _wire(design_scripted=2)
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    _resume(graph, {"approved": False, "feedback": "hold scope", "drop_areas": ["interface specs"]})
    _resume(graph, {"approved": True, "feedback": ""})
    final = _resume(graph, {"approved": True, "feedback": ""})
    dispositions = final["design"]["dispositions"]
    assert len(dispositions) == 1
    assert dispositions[0]["source"] == "interface specs"
    assert dispositions[0]["review"] == "dropped"
    assert dispositions[0]["evidence_id"]
    areas = {c["area"] for c in final["design"]["coverage_matrix"]}
    assert areas == {"checkout"}


def test_vetoes_accumulate_across_multiple_revise_rounds(monkeypatch):
    """A later veto must not resurrect an earlier one: dropped_areas is a
    cumulative set, not a replacement (guards any future MAX_REVISE > 2)."""
    monkeypatch.setattr("orchestrator.MAX_REVISE", 3)
    _wire(design_scripted=3)
    AGENT_REGISTRY["whitebox"].llm = FakeLLM(
        {"whitebox analysis agent": [{"items": [
            {"source": "interface specs", "finding": "negative amount", "risk_hint": "refund"},
            {"source": "boundary values", "finding": "zero amount", "risk_hint": "free refund"},
        ]}]}
    )
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    out = _resume(graph, {"approved": False, "feedback": "drop specs", "drop_areas": ["interface specs"]})
    assert out["__interrupt__"]
    out = _resume(graph, {"approved": False, "feedback": "drop boundaries", "drop_areas": ["boundary values"]})
    assert out["__interrupt__"]
    _resume(graph, {"approved": True, "feedback": ""})
    final = _resume(graph, {"approved": True, "feedback": ""})
    assert final["design_attempts"] == 3
    assert set(final["dropped_areas"]) == {"interface specs", "boundary values"}
    assert {c["area"] for c in final["design"]["coverage_matrix"]} == {"checkout"}
    assert {d["source"]: d["review"] for d in final["design"]["dispositions"]} == {
        "interface specs": "dropped", "boundary values": "dropped",
    }


def test_upholding_a_rebuttal_on_approval_drops_the_contested_row():
    """The natural arbitration path: approve the design while upholding the
    rebuttal (drop_areas). The veto is reflected into the design that flows to
    cases and gets persisted — no revise round needed to make it stick."""
    _wire()
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    out = _resume(graph, {"approved": True, "feedback": "", "drop_areas": ["interface specs"]})
    assert out["__interrupt__"]  # straight to the cases gate, no revise
    final = _resume(graph, {"approved": True, "feedback": ""})
    assert final["outcome"] == "report"
    assert final["design_attempts"] == 1  # upheld on approval, not via a revise
    areas = {c["area"] for c in final["design"]["coverage_matrix"]}
    assert areas == {"checkout"}
    dispositions = final["design"]["dispositions"]
    assert len(dispositions) == 1 and dispositions[0]["review"] == "dropped"


def test_disposition_is_deterministic_and_adds_no_llm_call():
    """The disposition policy is design-owned and deterministic: it answers
    evidence without spending a call, so the section 11 bound holds with
    dispositions present (4 happy-path calls, not 5)."""
    _wire()
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    _resume(graph, {"approved": True, "feedback": ""})
    final = _resume(graph, {"approved": True, "feedback": ""})
    assert final["design"]["dispositions"]  # answered...
    assert len(AGENT_REGISTRY["test_design"].llm.calls) == 1  # ...with no extra call
    assert len(AGENT_REGISTRY["whitebox"].llm.calls) == 1


def test_whitebox_failure_yields_empty_dispositions():
    _wire()
    AGENT_REGISTRY["whitebox"].llm = FakeLLM({})
    graph = _graph()
    graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    _resume(graph, {"approved": True, "feedback": ""})
    final = _resume(graph, {"approved": True, "feedback": ""})
    assert final["design"]["dispositions"] == []  # absent evidence cannot conflict
