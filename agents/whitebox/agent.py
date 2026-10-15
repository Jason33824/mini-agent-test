"""Whitebox agent: static-analysis evidence for the design agent (parallel branch)."""
from langgraph.graph import END, StateGraph

from agents.artifacts import load_artifact
from agents.base import AgentState, BaseAgent


class WhiteboxAgent(BaseAgent):
    name = "whitebox"
    description = "Static analysis evidence: interface contracts, boundary values, branch points."

    SYSTEM = "You are the whitebox analysis agent. Output strict JSON: items (list of {source, finding, risk_hint})."

    def _analyze(self, state: AgentState) -> AgentState:
        user = f"PRD and interface specs:\n{state['input_artifact'].get('prd', '')}"
        artifact = self.client().complete_json(self.SYSTEM, user)
        artifact.update(
            {
                "kind": "evidence",
                "version": "v1",
                "task_id": state["task_id"],
                "idempotency_key": f"{state['task_id']}:evidence",
                "produced_by": self.name,
            }
        )
        return {**state, "output_artifact": artifact}

    def _validate(self, state: AgentState) -> AgentState:
        load_artifact(state["output_artifact"])
        return state

    def build_graph(self):
        g = StateGraph(AgentState)
        g.add_node("analyze", self._analyze)
        g.add_node("validate", self._validate)
        g.set_entry_point("analyze")
        g.add_edge("analyze", "validate")
        g.add_edge("validate", END)
        return g
