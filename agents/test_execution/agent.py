"""Test-execution agent: per-case run orchestration, explicit failure reporting."""
from langgraph.graph import END, StateGraph

from agents.base import AgentState, BaseAgent


class TestExecutionAgent(BaseAgent):
    name = "test_execution"
    description = "Runs approved cases and produces an ExecutionReport with per-case results."

    def _run(self, state: AgentState) -> AgentState:
        # W2 build-out: per-case retry; results mark failed cases explicitly.
        return {**state, "output_artifact": {"kind": "report", "version": "v1", "results": []}}

    def build_graph(self):
        g = StateGraph(AgentState)
        g.add_node("run_cases", self._run)
        g.set_entry_point("run_cases")
        g.add_edge("run_cases", END)
        return g
