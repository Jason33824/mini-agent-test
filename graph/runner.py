"""Graph assembly: 3 nodes, 1 conditional edge at the review gate."""
from langgraph.graph import END, StateGraph

from graph.nodes import generate_draft, refine, review_gate
from graph.state import PipelineState


def build_graph():
    g = StateGraph(PipelineState)
    g.add_node("generate_draft", generate_draft)
    g.add_node("review_gate", review_gate)
    g.add_node("refine", refine)
    g.set_entry_point("generate_draft")
    g.add_edge("generate_draft", "review_gate")
    g.add_conditional_edges(
        "review_gate",
        lambda s: "refine" if s["approved"] else END,
    )
    g.add_edge("refine", END)
    return g.compile()
