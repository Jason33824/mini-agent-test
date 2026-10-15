"""Common agent interface: every domain agent is a named subgraph over a shared state."""
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph


class AgentState(TypedDict, total=False):
    task_id: str
    input_artifact: dict[str, Any]
    output_artifact: dict[str, Any]
    feedback: str
    approved: bool


class BaseAgent:
    """A named graph with one job. Subclasses set name/description and implement build_graph."""

    name: str = ""
    description: str = ""

    def build_graph(self):
        raise NotImplementedError

    def compile(self):
        return self.build_graph().compile()


def simple_gate_node(state: AgentState) -> AgentState:
    """Human review gate placeholder. W2 wires this to a LangGraph interrupt()."""
    return {**state, "approved": False, "feedback": "gate not wired yet"}
