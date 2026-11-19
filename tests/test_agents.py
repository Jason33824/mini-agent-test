"""Per-agent suites: graphs compile, contracts hold, semantic invariants are enforced."""
import pytest

from agents.artifacts import evidence_fingerprint, load_artifact
from agents.llm import FakeLLM
from agents.registry import AGENT_REGISTRY


@pytest.fixture(autouse=True)
def _reset_llm_injection():
    yield
    for agent in AGENT_REGISTRY.values():
        agent.llm = None


def test_registry_has_four_domain_agents():
    assert set(AGENT_REGISTRY) == {"test_design", "whitebox", "test_case", "test_execution"}


def test_every_agent_graph_compiles():
    for name, agent in AGENT_REGISTRY.items():
        assert agent.compile() is not None, f"{name} graph failed to compile"


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
            "scope": ["checkout flow"],
            "risks": ["payment double-charge"],
            "coverage_matrix": [
                {"area": "payment", "risk": "double-charge", "covered_by": ["tc-1"]}
            ],
        }
    )
    assert art.kind == "design"


def test_artifact_loader_rejects_unknown_kind():
    with pytest.raises(ValueError):
        load_artifact({"kind": "poem", "version": "v1", "task_id": "t1", "idempotency_key": "k", "produced_by": "x"})


def test_legacy_v1_evidence_without_item_ids_still_loads():
    # Adding fingerprints must not break pre-fingerprint v1 artifacts or
    # checkpoints; merge backfills the missing identity deterministically.
    artifact = load_artifact({
        "kind": "evidence", "version": "v1", "task_id": "t1",
        "idempotency_key": "k1", "produced_by": "whitebox",
        "items": [{"source": "interface specs", "finding": "f", "risk_hint": "r"}],
    })
    assert artifact.items[0].id == ""


def test_test_design_produces_valid_artifact():
    AGENT_REGISTRY["test_design"].llm = FakeLLM(
        {
            "test-design agent": [
                {"scope": ["checkout"], "risks": ["double-charge"], "coverage_matrix": []}
            ]
        }
    )
    out = AGENT_REGISTRY["test_design"].compile().invoke({"task_id": "t1", "input_artifact": {"prd": "sample PRD"}})
    assert load_artifact(out["output_artifact"]).kind == "design"


def test_whitebox_produces_evidence_kind():
    item = {"source": "interface specs", "finding": "negative amounts accepted", "risk_hint": "negative refund"}
    AGENT_REGISTRY["whitebox"].llm = FakeLLM({"whitebox analysis agent": [{"items": [item]}]})
    out = AGENT_REGISTRY["whitebox"].compile().invoke({"task_id": "t1", "input_artifact": {"prd": "sample"}})
    artifact = load_artifact(out["output_artifact"])
    assert artifact.kind == "evidence"
    assert artifact.items[0].id == evidence_fingerprint("t1", item, 0)


def test_whitebox_ids_are_stable_and_position_disambiguates_duplicates():
    # identical findings still receive distinct ids by list position; a retry
    # with the same output reproduces exactly the same ids (no renumbering of
    # an earlier item when a same-source item appears later).
    item = {"source": "interface specs", "finding": "same finding", "risk_hint": "same risk"}
    AGENT_REGISTRY["whitebox"].llm = FakeLLM(
        {"whitebox analysis agent": [{"items": [item, item]}, {"items": [item, item]}]}
    )
    agent = AGENT_REGISTRY["whitebox"]
    first = agent.compile().invoke({"task_id": "t1", "input_artifact": {"prd": "sample"}})
    second = agent.compile().invoke({"task_id": "t1", "input_artifact": {"prd": "sample"}})
    ids_first = [i["id"] for i in first["output_artifact"]["items"]]
    ids_second = [i["id"] for i in second["output_artifact"]["items"]]
    assert ids_first == ids_second
    assert ids_first[0] != ids_first[1]
    assert ids_first == [evidence_fingerprint("t1", item, 0), evidence_fingerprint("t1", item, 1)]


def test_link_check_rejects_unlinked_cases():
    AGENT_REGISTRY["test_case"].llm = FakeLLM(
        {
            "test-case agent": [
                {
                    "items": [
                        {
                            "title": "orphan case",
                            "steps": ["step"],
                            "data": "-",
                            "assertion": "-",
                            "priority": "P0",
                            "design_ref": "no-such-area",
                        }
                    ]
                }
            ]
        }
    )
    design = {
        "scope": ["checkout"],
        "risks": [],
        "coverage_matrix": [{"area": "checkout", "risk": "double-charge", "covered_by": []}],
    }
    with pytest.raises(ValueError, match="not linked to design anchors"):
        AGENT_REGISTRY["test_case"].compile().invoke(
            {"task_id": "t1", "input_artifact": design}
        )
