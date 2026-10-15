"""Supervisor orchestrator: routes a task across domain agents with human gates between stages.

Topology (docs/DESIGN.md sections 4, 9, 10):

    START -> design ----\
    START -> whitebox --+-> merge_evidence -> design_gate -> cases -> cases_gate -> execution -> END
                                     \                                    \
                                      rejected(revise<2): back to handoff   rejected(exhausted): surface
                                      rejected(exhausted): surface

Phase 1: every handoff is an in-process subgraph call (ADR 0002). Phase 2 swaps
the handoff for redis-stream messages; the artifact contract is untouched.
"""
import copy
import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agents.artifacts import load_artifact
from agents.registry import AGENT_REGISTRY

logger = logging.getLogger(__name__)

MAX_REVISE = 2  # bounded revise loop (DESIGN.md section 10)


class OrchestratorState(TypedDict, total=False):
    task_id: str
    prd: str
    feedback: str
    evidence: dict[str, Any]  # WhiteboxEvidence (parallel branch, optional)
    design: dict[str, Any]
    cases: dict[str, Any]
    report: dict[str, Any]
    approved: bool
    design_attempts: int
    cases_attempts: int
    outcome: str  # "report" | "rejected_design" | "rejected_cases"


def _handoff(agent_name: str, input_builder, output_field: str, attempts_key: str, on_error: str | None = None):
    """Invoke a domain agent as a subgraph, then validate its artifact at the boundary.

    input_builder maps orchestrator state to the agent's input_artifact dict —
    raw text fields (prd) get wrapped; upstream artifacts (design, cases)
    pass through unchanged.
    """

    def node(state: OrchestratorState) -> OrchestratorState:
        agent = AGENT_REGISTRY[agent_name]
        payload = {
            "task_id": state["task_id"],
            "input_artifact": input_builder(state),
            "feedback": state.get("feedback", ""),
        }
        try:
            result = agent.compile().invoke(payload)
        except Exception:
            if on_error != "skip":
                raise
            # Whitebox degrades, never blocks (DESIGN.md section 2).
            logger.warning("whitebox branch failed; continuing without evidence", exc_info=True)
            update = {output_field: {}}
        else:
            artifact = result["output_artifact"]
            load_artifact(artifact)  # typed rejection at the boundary — never forwarded silently
            update = {output_field: artifact}  # feedback clearing happens at the fan-in node
        if attempts_key:  # parallel branches write no shared key (INVALID_CONCURRENT_GRAPH_UPDATE)
            update[attempts_key] = state.get(attempts_key, 0) + 1
        return update

    node.__name__ = f"handoff_{agent_name}"
    return node


def _merge_evidence(state: OrchestratorState) -> OrchestratorState:
    """Fan-in: fold whitebox evidence into the design's coverage matrix (orchestrator node, DESIGN.md section 9)."""
    # deepcopy: the orchestrator never mutates an upstream artifact — a shallow
    # copy shares the nested coverage_matrix list and a revise round would fold
    # the same evidence twice (breaks handoff idempotency).
    design = copy.deepcopy(state["design"])
    evidence = state.get("evidence") or {}
    for item in evidence.get("items", []):
        design.setdefault("coverage_matrix", []).append(
            {"area": item.get("source", "static"), "risk": item.get("risk_hint", ""), "covered_by": []}
        )
    load_artifact(design)  # merged artifact still honors the contract
    # feedback is consumed here too: revise rounds re-run only the design branch,
    # and clearing at the fan-in keeps parallel branches free of shared writes.
    return {"design": design, "feedback": ""}


def _gate(stage: str):
    """Suspend the graph until the API resumes it with a human decision."""

    def node(state: OrchestratorState) -> OrchestratorState:
        decision = interrupt({"stage": stage, "task_id": state["task_id"]})
        return {
            "approved": bool(decision.get("approved")),
            "feedback": decision.get("feedback", ""),
        }

    node.__name__ = f"gate_{stage}"
    return node


def _after_gate(stage: str, attempts_key: str, back_to: str, forward: str):
    def route(state: OrchestratorState) -> str:
        if state["approved"]:
            return forward
        if state.get(attempts_key, 1) >= MAX_REVISE:
            return f"surface_{stage}"  # a human decides what is next; the loop never spins silently
        return back_to

    route.__name__ = f"route_{stage}"
    return route


def _finish(state: OrchestratorState) -> OrchestratorState:
    return {"outcome": "report"}


def _surface(stage: str):
    def node(state: OrchestratorState) -> OrchestratorState:
        return {"outcome": f"rejected_{stage}"}

    node.__name__ = f"surface_{stage}"
    return node


def build_orchestrator(checkpointer=None):
    g = StateGraph(OrchestratorState)
    g.add_node("design", _handoff("test_design", lambda s: {"prd": s["prd"]}, "design", "design_attempts"))
    g.add_node("whitebox", _handoff("whitebox", lambda s: {"prd": s["prd"]}, "evidence", None, on_error="skip"))
    g.add_node("merge_evidence", _merge_evidence)
    g.add_node("design_gate", _gate("design"))
    g.add_node("surface_design", _surface("design"))
    g.add_node("cases", _handoff("test_case", lambda s: s["design"], "cases", "cases_attempts"))
    g.add_node("cases_gate", _gate("cases"))
    g.add_node("surface_cases", _surface("cases"))
    g.add_node("execution", _handoff("test_execution", lambda s: s["cases"], "report", "cases_attempts"))
    g.add_node("finish", _finish)

    g.add_edge(START, "design")  # fan-out: design and whitebox run in parallel
    g.add_edge(START, "whitebox")
    g.add_edge("design", "merge_evidence")  # fan-in: join before the gate
    g.add_edge("whitebox", "merge_evidence")
    g.add_edge("merge_evidence", "design_gate")
    g.add_conditional_edges(
        "design_gate",
        _after_gate("design", "design_attempts", "design", "cases"),
        {"cases": "cases", "design": "design", "surface_design": "surface_design", END: END},
    )
    g.add_edge("surface_design", END)
    g.add_edge("cases", "cases_gate")
    g.add_conditional_edges(
        "cases_gate",
        _after_gate("cases", "cases_attempts", "cases", "execution"),
        {"execution": "execution", "cases": "cases", "surface_cases": "surface_cases", END: END},
    )
    g.add_edge("surface_cases", END)
    g.add_edge("execution", "finish")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)
