"""Test-design agent.

The W1 single pipeline (draft -> review gate -> refine) lives on here as this
agent's internal graph — absorbed, not deleted (ADR 0002, consequence 3).
"""
from langgraph.graph import END, StateGraph

from agents.base import AgentState, BaseAgent, simple_gate_node


class TestDesignAgent(BaseAgent):
    name = "test_design"
    description = "Turns a PRD into a test design: scope, risks, coverage matrix."

    def _draft(self, state: AgentState) -> AgentState:
        # W2 build-out: LLM call, merges whitebox evidence into a DesignArtifact.
        return {**state, "output_artifact": {"kind": "design", "version": "v1", "scope": [], "risks": []}}

    def _revise(self, state: AgentState) -> AgentState:
        # W2 build-out: bounded revise loop over gate feedback.
        return {**state}

    def build_graph(self):
        g = StateGraph(AgentState)
        g.add_node("draft_design", self._draft)
        g.add_node("review_gate", simple_gate_node)
        g.add_node("revise", self._revise)
        g.set_entry_point("draft_design")
        g.add_edge("draft_design", "review_gate")
        g.add_conditional_edges(
            "review_gate",
            lambda s: "revise" if s["approved"] else END,
        )
        g.add_edge("revise", END)
        return g
