"""LLM access for all agents: one OpenAI-compatible client, injectable for tests.

Production: env-configured (MINI_AGENT_LLM_BASE_URL / MINI_AGENT_LLM_MODEL /
OPENAI_API_KEY) against any OpenAI-compatible endpoint. Tests: inject a
FakeLLM into the agent instance — no network, no env needed.

Concurrency: every real call passes one process-wide semaphore
(MINI_AGENT_LLM_MAX_CONCURRENCY, default 4) — a fan-out spike degrades to
queueing instead of tripping the provider's rate limiter.

Transient-failure policy follows the OpenAI SDK defaults: 408/409/429/5xx
and transport errors are retried (MINI_AGENT_LLM_MAX_RETRIES, default 2)
with exponential backoff + full jitter; a server Retry-After wins over the
computed delay. Everything else (400/401/422 ...) fails fast — retrying a
malformed prompt never helps.
"""
import copy
import json
import logging
import os
import random
import threading
import time
from typing import Any, ClassVar, Protocol

from tracing import span

_LLM_SLOTS = threading.BoundedSemaphore(
    int(os.environ.get("MINI_AGENT_LLM_MAX_CONCURRENCY", "4"))
)

logger = logging.getLogger("agents.llm")

# OpenAI SDK parity (their client defaults to 2 retries, 0.5s exponential
# backoff capped at 8s) — overridable for providers with tighter budgets
_MAX_RETRIES = int(os.environ.get("MINI_AGENT_LLM_MAX_RETRIES", "2"))


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str) -> dict[str, Any]: ...


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        _first, _, rest = text.partition("\n")
        text = rest[: rest.rfind("```")] if rest.rfind("```") != -1 else rest
    return text.strip()


class OpenAICompatLLM:
    """Standard OpenAI-compatible chat endpoint (deepseek, gpt-4o-mini, ...).

    One logical call = one llm.chat span: provider retries land on it as
    llm.retry_count instead of spawning sibling spans (OpenInference LLM kind,
    so the Phoenix UI renders the token counters natively).
    """

    # the OpenAI SDK's retryable set: rate limits, hiccups, server-side errors
    _TRANSIENT_STATUS: ClassVar[set[int]] = {408, 409, 429, 500, 502, 503, 504}
    _BACKOFF_INITIAL = 0.5
    _BACKOFF_CAP = 8.0
    _RETRY_AFTER_CAP = 60.0  # a server hint this long is still worth waiting for

    def __init__(self, model: str | None = None, base_url: str | None = None,
                 agent: str | None = None, max_retries: int | None = None):
        self.model = model or os.environ.get("MINI_AGENT_LLM_MODEL", "gpt-4o-mini")
        self.base_url = (
            base_url or os.environ.get("MINI_AGENT_LLM_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        # attribution tag for the llm.chat span: which agent burned the tokens
        # (DESIGN.md section 6 — "which agent burns money" without log archaeology)
        self.agent = agent or "shared"
        self.max_retries = _MAX_RETRIES if max_retries is None else max_retries
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set — no default key ships with this repo")

    def _transport(self, payload: dict[str, Any]):
        """One HTTP attempt — the seam tests replace to script a flaky provider."""
        import httpx  # lazy: the offline suites never need the network

        return httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=120,
        )

    def _retry_wait(self, resp, attempt: int) -> float:
        """Server hint first; otherwise exponential backoff with full jitter."""
        if resp is not None:
            try:
                return min(float((resp.headers.get("Retry-After") or "").strip()),
                           self._RETRY_AFTER_CAP)
            except ValueError:
                pass  # absent, empty, or HTTP-date form — fall back to computed backoff
        cap = min(self._BACKOFF_INITIAL * (2 ** attempt), self._BACKOFF_CAP)
        return random.uniform(0, cap)

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        import httpx

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        # global cap, not per-client: agents fan out in parallel
        with _LLM_SLOTS, span("llm.chat", {"llm.model_name": self.model, "agent.name": self.agent},
                              kind="LLM") as current:
            body = self._chat(payload, current, httpx)
            content = body["choices"][0]["message"]["content"]
        return json.loads(_strip_code_fence(content))

    def _chat(self, payload: dict[str, Any], current, httpx) -> dict[str, Any]:
        attempt = 0
        while True:
            try:
                resp = self._transport(payload)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise
                wait = self._retry_wait(None, attempt)
                logger.warning("llm transport error (%s), retry %d/%d in %.2fs",
                               type(exc).__name__, attempt + 1, self.max_retries, wait)
            else:
                if resp.status_code not in self._TRANSIENT_STATUS:
                    resp.raise_for_status()
                    body = resp.json()
                    # usage arrives only in the response — recorded onto the live span
                    usage = body.get("usage") or {}
                    for key, attr in (("prompt_tokens", "llm.token_count.prompt"),
                                      ("completion_tokens", "llm.token_count.completion"),
                                      ("total_tokens", "llm.token_count.total")):
                        if key in usage:
                            current.set_attribute(attr, usage[key])
                    current.set_attribute("llm.retry_count", attempt)
                    return body
                if attempt >= self.max_retries:
                    # budget spent: surface the provider error, don't mask the status
                    resp.raise_for_status()
                wait = self._retry_wait(resp, attempt)
                logger.warning("llm got %d, retry %d/%d in %.2fs",
                               resp.status_code, attempt + 1, self.max_retries, wait)
            time.sleep(wait)
            attempt += 1


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


def get_llm(agent_name: str | None = None) -> LLMClient:
    """Model routing (DESIGN.md section 11): per-agent env override, else the shared default.

    MINI_AGENT_LLM_MODEL_<AGENT> (e.g. MINI_AGENT_LLM_MODEL_TEST_DESIGN) pins
    one agent to a model — strong model for generation, cheap for verdicts.
    """
    if agent_name:
        override = os.environ.get(f"MINI_AGENT_LLM_MODEL_{agent_name.upper()}")
        if override:
            return OpenAICompatLLM(model=override, agent=agent_name)
    global _default
    if _default is None:
        _default = OpenAICompatLLM()
    return _default


def set_llm(client: LLMClient | None) -> None:
    global _default
    _default = client
