from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from prometheus_client import REGISTRY

from llmserve.obs import metrics as m
from llmserve.obs.demo_app import Config, create_app
from llmserve.obs.genai import LLMError, chat
from llmserve.obs.telemetry import JsonFormatter, current_ids


def _sse(text: str, prompt_tokens: int = 40, completion_tokens: int = 9) -> bytes:
    events = [
        {"id": "cmpl-1", "model": "m", "choices": [{"delta": {"content": text}, "finish_reason": None}]},
        {"id": "cmpl-1", "model": "m", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}},
    ]
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_cost_estimate_uses_reference_prices():
    assert m.cost_usd(1_000_000, 0) == pytest.approx(m.PRICE_IN_PER_M)
    assert m.cost_usd(0, 1_000_000) == pytest.approx(m.PRICE_OUT_PER_M)


def test_no_active_span_means_no_ids_and_no_exemplar():
    assert current_ids() == ("", "") and m.exemplar() is None


def test_exemplar_carries_the_active_trace_id(spans):
    with trace.get_tracer("t").start_as_current_span("s"):
        tid, sid = current_ids()
        assert len(tid) == 32 and len(sid) == 16 and m.exemplar() == {"trace_id": tid}


def test_json_logs_carry_trace_correlation_and_extra_fields(spans):
    record_logger = logging.getLogger("fmt-test")
    with trace.get_tracer("t").start_as_current_span("s"):
        tid, _ = current_ids()
        rec = record_logger.makeRecord(
            "fmt-test", logging.WARNING, "f", 1, "hello %s", ("world",), None, extra={"stage": "answer"}
        )
        doc = json.loads(JsonFormatter().format(rec))
    assert doc["msg"] == "hello world" and doc["trace_id"] == tid and doc["stage"] == "answer"
    assert doc["level"] == "WARNING" and doc["ts"].endswith("Z")


def test_chat_records_genai_span_attributes_and_metrics(spans):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions" and json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=_sse("hello"))

    before = _sample("llmapp_llm_tokens_total", model="m", stage="answer", direction="input")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await chat(
                client,
                base_url="http://stub:8020",
                model="m",
                stage="answer",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=20,
            )

    import asyncio

    res = asyncio.run(go())
    assert (res.text, res.input_tokens, res.output_tokens, res.finish_reason) == ("hello", 40, 9, "stop")
    span = next(s for s in spans.get_finished_spans() if s.name == "chat m")
    attrs = span.attributes
    assert attrs["gen_ai.operation.name"] == "chat" and attrs["gen_ai.request.model"] == "m"
    assert attrs["gen_ai.usage.input_tokens"] == 40 and attrs["gen_ai.usage.output_tokens"] == 9
    assert list(attrs["gen_ai.response.finish_reasons"]) == ["stop"] and attrs["server.address"] == "stub"
    assert not any(e.name == "gen_ai.choice" for e in span.events)  # content capture is opt-in, off by default
    assert _sample("llmapp_llm_tokens_total", model="m", stage="answer", direction="input") == before + 40


def test_chat_failure_is_classified_and_marks_the_span(spans):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("stalled", request=request)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await chat(client, base_url="http://stub:8020", model="m", stage="answer", messages=[], max_tokens=5)

    import asyncio

    with pytest.raises(LLMError) as err:
        asyncio.run(go())
    assert err.value.error_type == "timeout" and err.value.retryable
    span = next(s for s in spans.get_finished_spans() if s.name == "chat m")
    assert span.attributes["error.type"] == "timeout" and span.status.status_code.name == "ERROR"


def _llm_stub(calls: list[str], stall_when_fault: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["messages"][0]["content"][:12])
        if stall_when_fault and request.extensions["timeout"]["read"] < 0.1:
            raise httpx.ReadTimeout("stalled", request=request)
        return httpx.Response(200, content=_sse("an answer"))

    return httpx.MockTransport(handler)


def test_ask_runs_two_llm_stages_and_exposes_metrics(spans):
    calls: list[str] = []
    app = create_app(Config(llm_base_url="http://stub:8020", llm_model="m"), _llm_stub(calls), setup_telemetry=False)
    before = _sample("llmapp_http_requests_total", method="POST", route="/ask", status="200")
    with TestClient(app) as client:
        r = client.post("/ask", json={"question": "What is a KV cache?"})
        assert (
            r.status_code == 200 and r.headers["x-trace-id"] == r.json()["trace_id"] and len(r.json()["trace_id"]) == 32
        )
        body = r.json()
        assert body["answer"] == "an answer" and set(body["stage_ms"]) == {"answer", "summarize"}
        assert body["tokens"] == {"input": 80, "output": 18}
        assert client.get("/healthz").json() == {"status": "ok"}
        metrics_text = client.get("/metrics").text
    assert len(calls) == 2
    assert 'llmapp_http_requests_total{method="POST",route="/ask",status="200"}' in metrics_text
    assert _sample("llmapp_http_requests_total", method="POST", route="/ask", status="200") == before + 1
    names = {s.name for s in spans.get_finished_spans()}
    assert {"invoke_agent gateway-demo", "agent.answer", "agent.summarize", "chat m"} <= names


def test_ask_validates_input_and_uses_route_templates_not_raw_urls(spans):
    app = create_app(Config(llm_model="m"), _llm_stub([]), setup_telemetry=False)
    with TestClient(app) as client:
        assert client.post("/ask", json={"question": "x"}).status_code == 422
        assert client.get("/nope/12345").status_code == 404
    assert _sample("llmapp_http_requests_total", method="GET", route="unmatched", status="404") >= 1
    assert "12345" not in "".join(str(s.labels) for f in REGISTRY.collect() for s in f.samples)


def test_stalled_backend_fault_returns_504_and_counts_an_error(spans):
    calls: list[str] = []
    cfg = Config(llm_base_url="http://stub:8020", llm_model="m", faults_enabled=True, llm_max_retries=1)
    app = create_app(cfg, _llm_stub(calls), setup_telemetry=False)
    errors_before = _sample("llmapp_requests_total", outcome="error")
    with TestClient(app) as client:
        r = client.post("/ask", json={"question": "What is prefix caching?"}, headers={"X-Fault": "llm_timeout"})
    assert r.status_code == 504 and r.json()["error"] == "llm_timeout" and r.json()["trace_id"]
    assert len(calls) == 2  # one retry
    assert _sample("llmapp_requests_total", outcome="error") == errors_before + 1
    root = next(s for s in spans.get_finished_spans() if s.name == "invoke_agent gateway-demo")
    assert root.attributes["error.type"] == "llm_timeout" and root.attributes["app.fault"] == "llm_timeout"


def test_fault_header_is_ignored_unless_faults_are_enabled(spans):
    app = create_app(Config(llm_model="m", faults_enabled=False), _llm_stub([]), setup_telemetry=False)
    with TestClient(app) as client:
        r = client.post("/ask", json={"question": "What is prefix caching?"}, headers={"X-Fault": "llm_timeout"})
    assert r.status_code == 200


def test_upstream_5xx_becomes_502(spans):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "overloaded"})

    cfg = Config(llm_model="m", llm_max_retries=0)
    app = create_app(cfg, httpx.MockTransport(handler), setup_telemetry=False)
    with TestClient(app) as client:
        r = client.post("/ask", json={"question": "What is prefix caching?"})
    assert r.status_code == 502 and r.json()["error"] == "llm_http_5xx"
