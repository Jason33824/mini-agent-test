"""Phoenix tracing: one trace tree per task, manual spans only.

A production lesson baked in: ``auto_instrument=False``. The LangChain/
OpenInference auto-instrumentors wrap every LangGraph internal Runnable and
emit a parallel duplicate of the manual span tree. The vocabulary here is
the domain's: task root -> agent handoff -> llm call (with token counts),
which is what "follow one task across all agents" needs to answer.

Degrades to no-ops offline: PHOENIX_URL unset (test suites, bare local runs)
skips provider registration and the OTel API's default tracer returns
non-recording spans — the call sites are unconditional and stay cheap.
"""
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

logger = logging.getLogger("tracing")

# OpenInference semantic convention: the Phoenix UI routes span rendering
# (LLM pages with token counters, chain pages with inputs/outputs) on this
# attribute — untyped spans still export but show as generic rows.
SPAN_KIND = "openinference.span.kind"

_initialized = False


def init_tracing() -> None:
    """Register the Phoenix exporter once, if configured. Never raises."""
    global _initialized
    if _initialized:
        return
    _initialized = True
    base_url = os.environ.get("PHOENIX_URL", "")
    if not base_url:
        return
    try:
        from phoenix.otel import register

        register(
            project_name=os.environ.get("PHOENIX_PROJECT", "mini-agent-test"),
            endpoint=base_url.rstrip("/") + "/v1/traces",
            auto_instrument=False,  # manual spans only — see module docstring
            verbose=False,
        )
        logger.info("phoenix tracing registered: %s", base_url)
    except Exception as exc:  # noqa: BLE001 — observability must not kill the run
        logger.warning("phoenix init failed, continuing without tracing: %s", exc)


def get_tracer() -> trace.Tracer:
    init_tracing()
    return trace.get_tracer("mini-agent-test")


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None,
         kind: str | None = None) -> Iterator[trace.Span]:
    """One span, closed on exit — a no-op when unrecorded.

    Attributes is a plain dict (not **kwargs): OTel semantic keys carry dots
    ("task.id", "llm.token_count.prompt"), which are illegal Python names.
    Kind is the OpenInference span kind ("CHAIN" for task/handoff nodes,
    "LLM" for provider calls). Yields the span so call sites can record
    attributes discovered mid-flight (provider usage counts arrive only in
    the response).
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as current:
        if kind is not None:
            current.set_attribute(SPAN_KIND, kind)
        for key, value in (attributes or {}).items():
            if value is not None:
                current.set_attribute(key, value)
        yield current
