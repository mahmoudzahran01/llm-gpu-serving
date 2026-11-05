"""demo_app: a small LLM-backed service, instrumented end to end, to practise observability against a real GPU server.

    POST /ask {"question": "..."}
       answer     : LLM call #1 (answer the question)
       summarize  : LLM call #2 (condense the answer to one sentence)

Observability built in (nothing here is "demo only": it is what you would ship):

* traces   OTel spans with the GenAI semantic conventions; W3C trace context propagated to vLLM, so vLLM's own
           server-side spans (queue time, time to first token) join the same trace
* metrics  Prometheus ``/metrics`` (OpenMetrics + exemplars): RED, tokens, estimated cost, TTFT
* logs     JSON lines carrying trace_id / span_id
* faults   ``X-Fault: llm_timeout`` request header (only when FAULTS_ENABLED=1) to rehearse a stalled backend

Run (the factory keeps importing this module free of side effects)::

    uvicorn llmserve.obs.demo_app:create_app --factory --port 8030 --no-access-log
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from prometheus_client.openmetrics import exposition as om_exposition
from pydantic import BaseModel, Field

from llmserve.obs import metrics as m
from llmserve.obs.genai import LLMError, LLMResult, chat
from llmserve.obs.telemetry import current_ids, setup_logging, setup_tracing

AGENT_NAME = "gateway-demo"
ANSWER_SYSTEM = "You are a helpful assistant. Answer the question clearly in at most five sentences."
SUMMARY_SYSTEM = "Condense the answer to ONE sentence of under 30 words. Reply in the same language as the question."
STAGES = ("answer", "summarize")
log = logging.getLogger("demo-app")
tracer = trace.get_tracer("llmserve.obs.demo_app")


@dataclass(frozen=True)
class Config:
    llm_base_url: str = os.getenv("LLM_BASE_URL", "http://localhost:8020")
    llm_model: str = os.getenv("LLM_MODEL", "Qwen/Qwen3-4B-Instruct-2507")
    llm_read_timeout_s: float = float(os.getenv("LLM_READ_TIMEOUT_S", "90"))
    llm_max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "1"))
    faults_enabled: bool = os.getenv("FAULTS_ENABLED", "0") == "1"
    log_file: str = os.getenv("APP_LOG_FILE", "")


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


class AppError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


@dataclass
class RunState:
    stage_s: dict[str, float] = field(default_factory=lambda: dict.fromkeys(STAGES, 0.0))
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    def add(self, res: LLMResult) -> None:
        self.input_tokens += res.input_tokens
        self.output_tokens += res.output_tokens
        self.cost += res.cost_usd


async def call_llm(
    client: httpx.AsyncClient,
    cfg: Config,
    stage: str,
    messages: list[dict],
    max_tokens: int,
    st: RunState,
    fault: str | None,
) -> LLMResult:
    """LLM call with retries on transient failure; each attempt is its own ``chat`` span."""
    timeout = httpx.Timeout(cfg.llm_read_timeout_s, connect=3.0)
    if fault == "llm_timeout" and stage == "answer":
        timeout = httpx.Timeout(connect=3.0, read=0.02, write=3.0, pool=3.0)  # simulate a stalled backend
    last: LLMError | None = None
    for attempt in range(cfg.llm_max_retries + 1):
        try:
            res = await chat(
                client,
                base_url=cfg.llm_base_url,
                model=cfg.llm_model,
                stage=stage,
                messages=messages,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            st.add(res)
            return res
        except LLMError as exc:
            last = exc
            trace.get_current_span().add_event("llm.retry", {"attempt": attempt + 1, "error.type": exc.error_type})
            log.warning(
                "llm call failed", extra={"stage": stage, "error_class": exc.error_type, "attempt": attempt + 1}
            )
            if not exc.retryable or attempt == cfg.llm_max_retries:
                break
            await asyncio.sleep(0.2 * (attempt + 1))
    assert last is not None
    raise AppError(
        504 if last.error_type == "timeout" else 502, f"llm_{last.error_type}", f"LLM call failed in {stage}"
    )


def finish(st: RunState, root: Any, outcome: str) -> None:
    root.set_attribute("app.outcome", outcome)
    m.APP_REQUESTS.labels(outcome).inc()
    for stage, secs in st.stage_s.items():
        if secs > 0:
            m.observe(m.STAGE_DURATION, secs, stage=stage)


async def run_pipeline(client: httpx.AsyncClient, cfg: Config, question: str, fault: str | None) -> dict:
    st = RunState()
    with tracer.start_as_current_span(f"invoke_agent {AGENT_NAME}") as root:
        root.set_attributes(
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": AGENT_NAME,
                "app.question.chars": len(question),
                "app.fault": fault or "none",
            }
        )
        try:
            t = time.perf_counter()
            with tracer.start_as_current_span("agent.answer"):
                answer = await call_llm(
                    client,
                    cfg,
                    "answer",
                    [{"role": "system", "content": ANSWER_SYSTEM}, {"role": "user", "content": question}],
                    400,
                    st,
                    fault,
                )
            st.stage_s["answer"] = time.perf_counter() - t

            t = time.perf_counter()
            with tracer.start_as_current_span("agent.summarize"):
                summary = await call_llm(
                    client,
                    cfg,
                    "summarize",
                    [
                        {"role": "system", "content": SUMMARY_SYSTEM},
                        {"role": "user", "content": f"Question: {question}\nAnswer: {answer.text}"},
                    ],
                    120,
                    st,
                    fault,
                )
            st.stage_s["summarize"] = time.perf_counter() - t
        except AppError as exc:
            root.set_attribute("error.type", exc.code)
            root.set_status(Status(StatusCode.ERROR, exc.detail))
            finish(st, root, "error")
            raise
        finish(st, root, "ok")
        root.set_attributes(
            {
                "app.cost_usd": round(st.cost, 8),
                "app.tokens.input": st.input_tokens,
                "app.tokens.output": st.output_tokens,
            }
        )
        log.info(
            "answer produced",
            extra={
                "tokens_in": st.input_tokens,
                "tokens_out": st.output_tokens,
                "cost_usd": round(st.cost, 8),
                **{f"{k}_ms": round(v * 1000) for k, v in st.stage_s.items()},
            },
        )
        return {
            "answer": answer.text.strip(),
            "summary": summary.text.strip(),
            "tokens": {"input": st.input_tokens, "output": st.output_tokens},
            "cost_usd": round(st.cost, 8),
            "stage_ms": {k: round(v * 1000, 1) for k, v in st.stage_s.items()},
            "trace_id": current_ids()[0],
        }


def create_app(
    cfg: Config | None = None, transport: httpx.AsyncBaseTransport | None = None, setup_telemetry: bool = True
) -> FastAPI:
    """Build the FastAPI app. Tests pass a stub ``transport`` and ``setup_telemetry=False``."""
    cfg = cfg or Config()
    if setup_telemetry:  # pragma: no cover - installs global exporters
        setup_logging(cfg.log_file or None)
        setup_tracing()
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        state["client"] = httpx.AsyncClient(limits=httpx.Limits(max_connections=256), transport=transport)
        log.info(
            "demo-app started",
            extra={"llm_base_url": cfg.llm_base_url, "llm_model": cfg.llm_model, "faults": cfg.faults_enabled},
        )
        yield
        await state["client"].aclose()

    app = FastAPI(title="llm-gateway-demo", lifespan=lifespan)
    if not HTTPXClientInstrumentor().is_instrumented_by_opentelemetry:
        HTTPXClientInstrumentor().instrument()
    FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,healthz", exclude_spans=["receive", "send"])

    @app.post("/ask")
    async def ask(req: AskRequest, request: Request):
        fault = request.headers.get("x-fault") if cfg.faults_enabled else None
        try:
            body = await run_pipeline(state["client"], cfg, req.question, fault)
            return JSONResponse(body, headers={"X-Trace-Id": body["trace_id"]})
        except AppError as exc:
            span = trace.get_current_span()
            span.set_attribute("error.type", exc.code)
            span.set_status(Status(StatusCode.ERROR, exc.detail))
            log.error("request failed", extra={"error_class": exc.code, "status": exc.status})
            tid = current_ids()[0]
            return JSONResponse(
                {"error": exc.code, "detail": exc.detail, "trace_id": tid},
                status_code=exc.status,
                headers={"X-Trace-Id": tid},
            )

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        """Last-resort handler: still a 5xx (counted by the SLO) but JSON with a trace id the user can quote."""
        tid = current_ids()[0]
        log.error("unhandled exception", exc_info=exc, extra={"error_class": type(exc).__name__})
        return JSONResponse(
            {"error": "internal_error", "detail": type(exc).__name__, "trace_id": tid},
            status_code=500,
            headers={"X-Trace-Id": tid},
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        # Exemplars only exist in the OpenMetrics exposition; Prometheus asks for it via the Accept header.
        if "application/openmetrics-text" in request.headers.get("accept", ""):
            return Response(om_exposition.generate_latest(REGISTRY), media_type=om_exposition.CONTENT_TYPE_LATEST)
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def http_metrics(request: Request, call_next):
        if request.url.path in ("/metrics", "/healthz"):
            return await call_next(request)
        m.INFLIGHT.inc()
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = route.path if route else "unmatched"  # template, never the raw URL (cardinality!)
            dur = time.perf_counter() - start
            m.HTTP_REQUESTS.labels(request.method, template, str(status)).inc()
            m.observe(m.HTTP_DURATION, dur, method=request.method, route=template)
            m.INFLIGHT.dec()
            log.info(
                "request completed", extra={"route": template, "status": status, "duration_ms": round(dur * 1000, 1)}
            )

    return app
