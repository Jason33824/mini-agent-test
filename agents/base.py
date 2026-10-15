"""Common agent interface: every domain agent is a named subgraph over a shared state.

Agents contain business nodes only — no human gates here. Gates belong to the
orchestrator (docs/DESIGN.md section 9), which keeps agents pure and makes
collaboration semantics testable in one place.
"""
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from agents.llm import LLMClient, get_llm


class AgentState(TypedDict, total=False):
    task_id: str
    input_artifact: dict[str, Any]
    output_artifact: dict[str, Any]
    feedback: str


class BaseAgent:
    """A named graph with one job. Subclasses set name/description and implement build_graph.

    LLM access is injectable: tests set .llm to a FakeLLM; production falls
    back to the env-configured default client.
    """

    name: str = ""
    description: str = ""

    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm

    def client(self) -> LLMClient:
        return self.llm or get_llm(self.name)  # per-agent model slot (DESIGN.md section 11)

    def build_graph(self):
        raise NotImplementedError

    def compile(self):
        return self.build_graph().compile()
