"""LLM access for all agents: one OpenAI-compatible client, injectable for tests.

Production: env-configured (MINI_AGENT_LLM_BASE_URL / MINI_AGENT_LLM_MODEL /
OPENAI_API_KEY) against any OpenAI-compatible endpoint. Tests: inject a
FakeLLM into the agent instance — no network, no env needed.
"""
import copy
import json
import os
from typing import Any, Protocol


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str) -> dict[str, Any]: ...


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first, _, rest = text.partition("\n")
        text = rest[: rest.rfind("```")] if rest.rfind("```") != -1 else rest
    return text.strip()


class OpenAICompatLLM:
    """Standard OpenAI-compatible chat endpoint (deepseek, gpt-4o-mini, ...)."""

    def __init__(self, model: str | None = None, base_url: str | None = None):
        self.model = model or os.environ.get("MINI_AGENT_LLM_MODEL", "gpt-4o-mini")
        self.base_url = (
            base_url or os.environ.get("MINI_AGENT_LLM_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set — no default key ships with this repo")

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        import httpx  # lazy: tests never touch the network

        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            timeout=120,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(_strip_code_fence(content))


class FakeLLM:
    """Scripted responses routed by substring match on the system prompt.

    scripted maps a system-prompt substring to a queue of canned responses;
    parallel agent branches consume independent queues, so fan-out order
    never matters (unlike a single ordered list).
    """

    def __init__(self, scripted: dict[str, list[dict[str, Any]]]):
        self.scripted = {k: list(v) for k, v in scripted.items()}
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        self.calls.append((system, user))
        for key, queue in self.scripted.items():
            if key in system and queue:
                return copy.deepcopy(queue.pop(0))
        raise AssertionError(f"FakeLLM: no scripted response matches {system[:60]!r}")


_default: LLMClient | None = None


def get_llm() -> LLMClient:
    global _default
    if _default is None:
        _default = OpenAICompatLLM()
    return _default


def set_llm(client: LLMClient | None) -> None:
    global _default
    _default = client
