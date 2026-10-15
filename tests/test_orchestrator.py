"""Orchestrator suites: collaboration sequence, gates, bounded revise loop, graceful degrade."""
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agents.llm import FakeLLM
from agents.registry import AGENT_REGISTRY
from agents.test_case.agent import TestCaseAgent
from agents.test_design.agent import TestDesignAgent
from agents.test_execution.agent import TestExecutionAgent
from agents.whitebox.agent import WhiteboxAgent
from orchestrator import MAX_REVISE, build_orchestrator

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


def test_whitebox_failure_degrades_never_blocks():
    _wire()
    AGENT_REGISTRY["whitebox"].llm = FakeLLM({})  # no scripted response -> raises
    graph = _graph()
    out = graph.invoke({"task_id": "t1", "prd": "checkout PRD"}, CFG)
    out = _resume(graph, {"approved": True, "feedback": ""})
    final = _resume(graph, {"approved": True, "feedback": ""})
    assert final["outcome"] == "report"
    areas = {c["area"] for c in final["design"]["coverage_matrix"]}
    assert areas == {"checkout"}  # design's own row only
