"""Supervisor orchestrator: routes a task across domain agents with human gates between stages.

Phase 1: every handoff is an in-process subgraph call (ADR 0002). Phase 2 swaps
the handoff for redis-stream messages; the artifact contract is untouched.
"""
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph


class OrchestratorState(TypedDict, total=False):
    task_id: str
    prd: str
    design: dict[str, Any]
    cases: dict[str, Any]
    report: dict[str, Any]
    approved: bool
    feedback: str


def _handoff(agent_name: str):
    def node(state: OrchestratorState) -> OrchestratorState:
        # W2 build-out: invoke agents.registry[agent_name] with the proper
        # input artifact, validate the returned artifact via agents.artifacts.
        return state

    node.__name__ = f"handoff_{agent_name}"
    return node


def _gate(stage: str):
    def node(state: OrchestratorState) -> OrchestratorState:
        # W2 wires this to interrupt(): the API serves the artifact, the human
        # answers, the graph resumes. Until then gates stop the flow (approved
        # defaults to False), which keeps the end-to-end skeleton honest.
        return {**state, "approved": False, "feedback": f"{stage} gate not wired yet"}

    node.__name__ = f"gate_{stage}"
    return node


def build_orchestrator():
    g = StateGraph(OrchestratorState)
    g.add_node("design", _handoff("test_design"))  # W1.5 build-out: parallel whitebox evidence fan-in
    g.add_node("design_gate", _gate("design"))
    g.add_node("cases", _handoff("test_case"))
    g.add_node("cases_gate", _gate("cases"))
    g.add_node("execution", _handoff("test_execution"))
    g.set_entry_point("design")
    g.add_edge("design", "design_gate")
    g.add_conditional_edges(
        "design_gate",
        lambda s: "cases" if s["approved"] else END,
    )
    g.add_edge("cases", "cases_gate")
    g.add_conditional_edges(
        "cases_gate",
        lambda s: "execution" if s["approved"] else END,
    )
    g.add_edge("execution", END)
    return g.compile()
