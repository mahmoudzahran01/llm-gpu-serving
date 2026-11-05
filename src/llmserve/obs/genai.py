"""One instrumented LLM call, following the OpenTelemetry GenAI semantic conventions.

Span: ``chat {model}``  (kind CLIENT)
  gen_ai.operation.name        = "chat"
  gen_ai.system / provider.name= "vllm"           (``gen_ai.system`` is the older name, still widely read)
  gen_ai.request.model / max_tokens / temperature
  gen_ai.response.id / model / finish_reasons
  gen_ai.usage.input_tokens / output_tokens
  server.address / server.port
  error.type                   on failure (+ exception event + ERROR status)
Opt-in content capture (prompt / completion) is a span *event*, off unless
``OTEL_GENAI_CAPTURE_CONTENT=true`` because prompts can contain customer data.

The call streams (SSE) so the client can measure **time-to-first-token**, which no
non-streaming call can see. Trace context is injected into the HTTP request by the httpx
instrumentation, so vLLM's own server-side spans join the same trace.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from llmserve.obs import metrics as m

tracer = trace.get_tracer("llmserve.obs.genai")

CAPTURE_CONTENT = os.getenv("OTEL_GENAI_CAPTURE_CONTENT", "false").lower() == "true"
CONTENT_LIMIT = 2000


class LLMError(Exception):
    """An LLM call failed; ``error_type`` is a bounded class suitable for a metric label."""

    def __init__(self, error_type: str, message: str, retryable: bool = True):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable


@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    finish_reason: str
    ttft_s: float
    duration_s: float
    response_id: str
    response_model: str
    cost_usd: float


def _clip(text: str) -> str:
    return text if len(text) <= CONTENT_LIMIT else text[:CONTENT_LIMIT] + "...[truncated]"


def _classify(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", True
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError)):
        return "connect_error", True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return (f"http_{code // 100}xx", code >= 500)
    return "unknown", False


async def chat(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    model: str,
    stage: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float = 0.0,
    timeout: httpx.Timeout | None = None,
    extra_body: dict | None = None,
) -> LLMResult:
    """Stream one chat completion and record span + metrics. Raises ``LLMError`` on failure."""
    parsed = urlparse(base_url)
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
        **(extra_body or {}),
    }
    with tracer.start_as_current_span(f"chat {model}", kind=SpanKind.CLIENT) as span:
        span.set_attributes(
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.system": "vllm",
                "gen_ai.provider.name": "vllm",
                "gen_ai.request.model": model,
                "gen_ai.request.max_tokens": max_tokens,
                "gen_ai.request.temperature": temperature,
                "gen_ai.output.type": "text",
                "server.address": parsed.hostname or "",
                "server.port": parsed.port or 80,
                "app.llm.stage": stage,
            }
        )
        if CAPTURE_CONTENT:
            span.add_event(
                "gen_ai.client.inference.operation.details",
                {"gen_ai.input.messages": _clip(json.dumps(messages[-2:], ensure_ascii=False))},
            )
        start = time.perf_counter()
        ttft = 0.0
        chunks: list[str] = []
        usage: dict = {}
        finish = ""
        resp_id = resp_model = ""
        try:
            async with client.stream("POST", f"{base_url}/v1/chat/completions", json=body, timeout=timeout) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    evt = json.loads(payload)
                    resp_id = evt.get("id", resp_id)
                    resp_model = evt.get("model", resp_model)
                    if evt.get("usage"):
                        usage = evt["usage"]
                    for choice in evt.get("choices", []):
                        piece = (choice.get("delta") or {}).get("content")
                        if piece:
                            if not ttft:
                                ttft = time.perf_counter() - start
                                span.add_event("first_token", {"ttft_ms": round(ttft * 1000, 1)})
                            chunks.append(piece)
                        finish = choice.get("finish_reason") or finish
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            etype, retryable = _classify(exc)
            duration = time.perf_counter() - start
            span.set_attribute("error.type", etype)
            span.record_exception(exc)  # span event "exception" with stack trace
            span.set_status(Status(StatusCode.ERROR, f"{etype}: {exc}"[:200]))
            m.LLM_REQUESTS.labels(model, stage, etype, "none").inc()
            m.observe(m.LLM_DURATION, duration, model=model, stage=stage)
            raise LLMError(etype, str(exc) or etype, retryable) from exc

        duration = time.perf_counter() - start
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))
        cost = m.cost_usd(in_tok, out_tok)
        text = "".join(chunks)
        span.set_attributes(
            {
                "gen_ai.response.id": resp_id,
                "gen_ai.response.model": resp_model,
                "gen_ai.response.finish_reasons": [finish or "unknown"],
                "gen_ai.usage.input_tokens": in_tok,
                "gen_ai.usage.output_tokens": out_tok,
                "app.llm.ttft_ms": round(ttft * 1000, 1),
                "app.llm.cost_usd": round(cost, 8),
            }
        )
        if CAPTURE_CONTENT:
            span.add_event(
                "gen_ai.choice", {"gen_ai.output.messages": _clip(text), "finish_reason": finish or "unknown"}
            )
        m.LLM_REQUESTS.labels(model, stage, "ok", finish or "unknown").inc()
        m.LLM_TOKENS.labels(model, stage, "input").inc(in_tok)
        m.LLM_TOKENS.labels(model, stage, "output").inc(out_tok)
        m.LLM_COST.labels(model, stage).inc(cost)
        m.observe(m.LLM_TTFT, ttft or duration, model=model, stage=stage)
        m.observe(m.LLM_DURATION, duration, model=model, stage=stage)
        return LLMResult(text, in_tok, out_tok, finish or "unknown", ttft, duration, resp_id, resp_model, cost)
