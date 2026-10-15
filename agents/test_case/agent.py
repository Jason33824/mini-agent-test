"""Test-case agent: approved DesignArtifact -> CaseArtifact, every case linked to the design."""
import json

from langgraph.graph import END, StateGraph

from agents.artifacts import load_artifact
from agents.base import AgentState, BaseAgent


class TestCaseAgent(BaseAgent):
    name = "test_case"
    description = "Turns an approved design into concrete test cases."

    SYSTEM = "You are the test-case agent. Output strict JSON: items (list of {title, steps, data, assertion, priority P0|P1|P2, design_ref})."

    def _draft_cases(self, state: AgentState) -> AgentState:
        design = state["input_artifact"]
        user = "Approved design:\n" + json.dumps(
            {k: design.get(k) for k in ("scope", "risks", "coverage_matrix")}, ensure_ascii=False
        )
        if state.get("feedback"):
            user += f"\n\nReviewer feedback to address: {state['feedback']}"
        artifact = self.client().complete_json(self.SYSTEM, user)
        artifact.update(
            {
                "kind": "cases",
                "version": "v1",
                "task_id": state["task_id"],
                "idempotency_key": f"{state['task_id']}:cases",
                "produced_by": self.name,
            }
        )
        return {**state, "output_artifact": artifact}

    def _link_check(self, state: AgentState) -> AgentState:
        """Semantic invariant (DESIGN.md section 8): every case links to a design item."""
        design = state["input_artifact"]
        anchors = set(design.get("scope", [])) | {c["area"] for c in design.get("coverage_matrix", [])}
        missing = [i["title"] for i in state["output_artifact"]["items"] if i.get("design_ref") not in anchors]
        if missing:
            raise ValueError(f"cases not linked to design anchors {sorted(anchors)}: {missing}")
        return state

    def _validate(self, state: AgentState) -> AgentState:
        load_artifact(state["output_artifact"])
        return state

    def build_graph(self):
        g = StateGraph(AgentState)
        g.add_node("draft_cases", self._draft_cases)
        g.add_node("link_check", self._link_check)
        g.add_node("validate", self._validate)
        g.set_entry_point("draft_cases")
        g.add_edge("draft_cases", "link_check")
        g.add_edge("link_check", "validate")
        g.add_edge("validate", END)
        return g
