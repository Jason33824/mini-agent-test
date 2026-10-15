"""Node implementations. W1 build-out replaces placeholders with real LLM calls."""


def generate_draft(state):
    """Node 1: PRD -> draft test cases (LLM call lands in W1 build-out)."""
    return {**state, "draft": "[W1 placeholder] draft test cases"}


def review_gate(state):
    """Node 2: the single human-in-the-loop checkpoint.

    W2/W3 wires this to a LangGraph interrupt(): the job suspends, the API
    serves the draft to the reviewer, and the graph resumes on approval.
    """
    return {**state, "approved": False, "review_feedback": "gate not wired yet"}


def refine(state):
    """Node 3: apply review feedback -> final output (LLM call lands in W1 build-out)."""
    if not state.get("approved"):
        return {**state, "final": ""}
    return {**state, "final": "[W1 placeholder] refined test cases"}
