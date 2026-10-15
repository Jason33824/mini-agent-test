"""Orchestrator suites: gate semantics — rejection terminates, approval proceeds."""
from orchestrator import build_orchestrator


def test_orchestrator_compiles():
    assert build_orchestrator() is not None


def test_first_gate_terminates_flow_when_not_approved():
    app = build_orchestrator()
    out = app.invoke({"task_id": "t1", "prd": "sample PRD", "approved": False})
    # design gate is not wired yet (approved stays False) -> flow must stop there
    assert "cases" not in out
    assert out["feedback"] == "design gate not wired yet"
