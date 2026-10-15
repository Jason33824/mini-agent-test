"""Test-execution agent: per-case runs -> ExecutionReport with explicit failure marking."""
import json

from langgraph.graph import END, StateGraph

from agents.artifacts import load_artifact
from agents.base import AgentState, BaseAgent


class TestExecutionAgent(BaseAgent):
    name = "test_execution"
    description = "Runs approved cases and produces an ExecutionReport with per-case results."

    SYSTEM = "You are the test-execution agent. Output strict JSON: results (list of {title, passed, detail}). Mark failed cases explicitly."

    def _run_cases(self, state: AgentState) -> AgentState:
        user = "Approved cases:\n" + json.dumps(state["input_artifact"].get("items", []), ensure_ascii=False)
        artifact = self.client().complete_json(self.SYSTEM, user)
        artifact.update(
            {
                "kind": "report",
                "version": "v1",
                "task_id": state["task_id"],
                "idempotency_key": f"{state['task_id']}:report",
                "produced_by": self.name,
            }
        )
        return {**state, "output_artifact": artifact}

    def _validate(self, state: AgentState) -> AgentState:
        load_artifact(state["output_artifact"])
        return state

    def build_graph(self):
        g = StateGraph(AgentState)
        g.add_node("run_cases", self._run_cases)
        g.add_node("validate", self._validate)
        g.set_entry_point("run_cases")
        g.add_edge("run_cases", "validate")
        g.add_edge("validate", END)
        return g
    # W3: per-case retry with bounded attempts; failed cases stay explicit in the report.
