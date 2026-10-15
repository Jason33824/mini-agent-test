"""Per-agent suites: every agent compiles; contracts reject unknown versions."""
import pytest

from agents.artifacts import load_artifact
from agents.registry import AGENT_REGISTRY


def test_registry_has_four_domain_agents():
    assert set(AGENT_REGISTRY) == {"test_design", "whitebox", "test_case", "test_execution"}


def test_every_agent_graph_compiles():
    for name, agent in AGENT_REGISTRY.items():
        assert agent.compile() is not None, f"{name} graph failed to compile"


def test_gate_stops_test_design_when_not_approved():
    app = AGENT_REGISTRY["test_design"].compile()
    out = app.invoke({"task_id": "t1", "input_artifact": {"prd": "sample"}, "approved": False})
    assert out["approved"] is False


def test_artifact_loader_rejects_unknown_version():
    with pytest.raises(ValueError):
        load_artifact({"kind": "design", "version": "v9"})


def test_artifact_loader_accepts_v1_design():
    art = load_artifact(
        {
            "kind": "design",
            "version": "v1",
            "task_id": "t1",
            "idempotency_key": "k1",
            "produced_by": "test_design",
            "scope": [],
            "risks": [],
        }
    )
    assert art.kind == "design"
