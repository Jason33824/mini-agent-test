"""Whitebox agent: static-analysis evidence for the design agent (parallel branch)."""
from langgraph.graph import END, StateGraph

from agents.artifacts import evidence_fingerprint, load_artifact
from agents.base import AgentState, BaseAgent


class WhiteboxAgent(BaseAgent):
    name = "whitebox"
    description = "Static analysis evidence: interface contracts, boundary values, branch points."

    SYSTEM = "You are the whitebox analysis agent. Output strict JSON: items (list of {source, finding, risk_hint})."

    def _analyze(self, state: AgentState) -> AgentState:
        user = f"PRD and interface specs:\n{state['input_artifact'].get('prd', '')}"
        artifact = self.client().complete_json(self.SYSTEM, user)
        # Finding identity is platform-owned, not model-generated: deterministic
        # per task + content + list position, so retries/revises preserve it and
        # same-source findings remain distinct for disposition and audit. Copy
        # each item first: aliased dicts in a scripted response must not share
        # the id written by a later list position.
        items = artifact.get("items") or []
        if isinstance(items, list):
            artifact["items"] = [dict(item) if isinstance(item, dict) else item for item in items]
            for index, item in enumerate(artifact["items"]):
                if isinstance(item, dict):
                    item["id"] = evidence_fingerprint(state["task_id"], item, index)
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
