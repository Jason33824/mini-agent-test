"""Graph-level tests. These are the seed of the W4 CI gate."""
from graph.runner import build_graph


def test_graph_compiles():
    app = build_graph()
    assert app is not None


def test_gate_stops_pipeline_when_not_approved():
    app = build_graph()
    out = app.invoke(
        {
            "prd": "sample PRD",
            "draft": "",
            "review_feedback": "",
            "approved": False,
            "final": "",
        }
    )
    assert out["approved"] is False
    assert out["final"] == ""
