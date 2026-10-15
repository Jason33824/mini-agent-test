"""Agent registry: the orchestrator's roster."""
from agents.base import BaseAgent
from agents.test_case.agent import TestCaseAgent
from agents.test_design.agent import TestDesignAgent
from agents.test_execution.agent import TestExecutionAgent
from agents.whitebox.agent import WhiteboxAgent

AGENT_REGISTRY: dict[str, BaseAgent] = {
    a.name: a
    for a in (TestDesignAgent(), WhiteboxAgent(), TestCaseAgent(), TestExecutionAgent())
}
