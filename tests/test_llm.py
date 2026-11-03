"""OpenAICompatLLM resilience suites: the retry policy, without a network.

The provider is scripted through the _transport seam — status codes and
Retry-After headers arrive exactly as httpx would hand them — and sleeps are
recorded instead of slept, so backoff bounds are asserted, not waited out.
"""
import httpx
import pytest

from agents import llm as llm_mod
from agents.llm import OpenAICompatLLM

OK_BODY = {"choices": [{"message": {"content": '{"ok": true}'}}],
           "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}


class Resp:
    """Minimal httpx.Response stand-in: what _chat actually touches."""

    def __init__(self, status_code: int = 200, body: dict | None = None,
                 headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("scripted", request=None, response=None)


def _llm(monkeypatch, script, max_retries: int = 2):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    client = OpenAICompatLLM(model="m", base_url="http://provider.invalid/v1",
                             max_retries=max_retries)
    calls: list[dict] = []
    slept: list[float] = []

    def transport(payload):
        calls.append(payload)
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client._transport = transport
    monkeypatch.setattr(llm_mod.time, "sleep", slept.append)
    return client, calls, slept


def test_transient_5xx_is_retried_then_succeeds(monkeypatch):
    client, calls, slept = _llm(monkeypatch, [Resp(503), Resp(200, OK_BODY)])
    assert client.complete_json("sys", "usr") == {"ok": True}
    assert len(calls) == 2
    # first backoff: <= 0.5s initial cap, full jitter (could be any 0..0.5)
    assert len(slept) == 1 and 0 <= slept[0] <= 0.5


def test_retry_after_header_wins_over_computed_backoff(monkeypatch):
    client, _, slept = _llm(
        monkeypatch, [Resp(429, headers={"Retry-After": "3"}), Resp(200, OK_BODY)])
    client.complete_json("s", "u")
    assert slept == [3.0]


def test_retry_after_hint_is_capped(monkeypatch):
    # a "come back in an hour" from the provider must not park a worker slot
    client, _, slept = _llm(
        monkeypatch, [Resp(429, headers={"Retry-After": "3600"}), Resp(200, OK_BODY)])
    client.complete_json("s", "u")
    assert slept == [60.0]


def test_client_errors_fail_fast_without_retry(monkeypatch):
    client, calls, slept = _llm(monkeypatch, [Resp(400)])
    with pytest.raises(httpx.HTTPStatusError):
        client.complete_json("s", "u")
    assert len(calls) == 1 and slept == []


def test_retry_budget_exhausted_surfaces_last_status(monkeypatch):
    client, calls, slept = _llm(monkeypatch, [Resp(503), Resp(503), Resp(503)])
    with pytest.raises(httpx.HTTPStatusError):
        client.complete_json("s", "u")
    assert len(calls) == 3  # initial + 2 retries, then the provider error stands
    assert len(slept) == 2


def test_transport_errors_are_retried(monkeypatch):
    client, calls, _ = _llm(
        monkeypatch, [httpx.ConnectError("refused"), Resp(200, OK_BODY)])
    assert client.complete_json("s", "u") == {"ok": True}
    assert len(calls) == 2
