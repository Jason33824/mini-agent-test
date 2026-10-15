"""Whitebox agent — parallel evidence producer (never a blocking stage; DESIGN.md section 2)."""
from langgraph.graph import END, StateGraph

from agents.base import AgentState, BaseAgent


class WhiteboxAgent(BaseAgent):
    name = "whitebox"
    description = "Static analysis evidence: interface contracts, boundary values, branch points."

    def _analyze(self, state: AgentState) -> AgentState:
        # W2 build-out: LLM call over interface specs -> WhiteboxEvidence.
        return {**state, "output_artifact": {"kind": "design", "version": "v1", "evidence": []}}

    def build_graph(self):
        g = StateGraph(AgentState)
        g.add_node("analyze", self._analyze)
        g.set_entry_point("analyze")
        g.add_edge("analyze", END)
        return g
