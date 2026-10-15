"""Test-case agent: DesignArtifact -> CaseArtifact (steps, data, assertions, priority)."""
from langgraph.graph import END, StateGraph

from agents.base import AgentState, BaseAgent, simple_gate_node


class TestCaseAgent(BaseAgent):
    name = "test_case"
    description = "Turns an approved design into concrete test cases."

    def _draft_cases(self, state: AgentState) -> AgentState:
        # W2 build-out: LLM call; every case must carry a design_ref.
        return {**state, "output_artifact": {"kind": "cases", "version": "v1", "items": []}}

    def build_graph(self):
        g = StateGraph(AgentState)
        g.add_node("draft_cases", self._draft_cases)
        g.add_node("review_gate", simple_gate_node)
        g.set_entry_point("draft_cases")
        g.add_edge("draft_cases", "review_gate")
        g.add_edge("review_gate", END)  # W2: conditional refine loop like test-design
        return g
