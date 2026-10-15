"""Test-design agent: PRD -> validated DesignArtifact (scope, risks, coverage matrix)."""
from langgraph.graph import END, StateGraph

from agents.artifacts import load_artifact
from agents.base import AgentState, BaseAgent


class TestDesignAgent(BaseAgent):
    name = "test_design"
    description = "Turns a PRD into a test design: scope, risks, coverage matrix."

    SYSTEM = "You are the test-design agent. Output strict JSON: scope (list of str), risks (list of str), coverage_matrix (list of {area, risk, covered_by})."

    def _draft(self, state: AgentState) -> AgentState:
        user = f"PRD:\n{state['input_artifact'].get('prd', '')}"
        if state.get("feedback"):
            user += f"\n\nReviewer feedback to address: {state['feedback']}"
        artifact = self.client().complete_json(self.SYSTEM, user)
        artifact.update(
            {
                "kind": "design",
                "version": "v1",
                "task_id": state["task_id"],
                "idempotency_key": f"{state['task_id']}:design",
                "produced_by": self.name,
            }
        )
        return {**state, "output_artifact": artifact}

    def _validate(self, state: AgentState) -> AgentState:
        load_artifact(state["output_artifact"])  # raises on missing/unknown fields
        return state

    def build_graph(self):
        g = StateGraph(AgentState)
        g.add_node("draft_design", self._draft)
        g.add_node("validate", self._validate)
        g.set_entry_point("draft_design")
        g.add_edge("draft_design", "validate")
        g.add_edge("validate", END)
        return g
