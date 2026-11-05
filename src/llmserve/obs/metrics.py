"""Prometheus metrics for the demo service.

Cardinality rules followed here (see obs/README.md):

* labels are drawn from small fixed sets (route *template*, stage, outcome, error class);
* NEVER a label for user id, question text, trace id or raw URL;
* the trace id travels as an *exemplar* (one sample per bucket), not as a label.
"""

from __future__ import annotations

import os

from prometheus_client import Counter, Gauge, Histogram

from llmserve.obs.telemetry import current_ids

# Reference price used ONLY to turn tokens into an "estimated cost" signal. Self-hosted GPUs have no per-token
# bill; this stands in for "what a hosted API of this size would charge" so cost-per-request panels and budget
# alerts can be practised. Override with env vars.
PRICE_IN_PER_M = float(os.getenv("PRICE_IN_USD_PER_M_TOKENS", "0.10"))
PRICE_OUT_PER_M = float(os.getenv("PRICE_OUT_USD_PER_M_TOKENS", "0.40"))

HTTP_BUCKETS = (0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60, 120)
STAGE_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60)
TTFT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60)

HTTP_REQUESTS = Counter("llmapp_http_requests_total", "HTTP requests handled", ["method", "route", "status"])
HTTP_DURATION = Histogram(
    "llmapp_http_request_duration_seconds",
    "End-to-end HTTP request latency",
    ["method", "route"],
    buckets=HTTP_BUCKETS,
)
INFLIGHT = Gauge("llmapp_http_inflight_requests", "Requests currently being processed")

APP_REQUESTS = Counter("llmapp_requests_total", "Answers by outcome (ok | error)", ["outcome"])
STAGE_DURATION = Histogram(
    "llmapp_stage_duration_seconds",
    "Latency of each pipeline stage",
    ["stage"],  # answer | summarize
    buckets=STAGE_BUCKETS,
)

LLM_REQUESTS = Counter(
    "llmapp_llm_requests_total",
    "LLM calls by stage / outcome / finish reason",
    ["model", "stage", "outcome", "finish_reason"],
)
LLM_TOKENS = Counter(
    "llmapp_llm_tokens_total",
    "LLM tokens consumed",
    ["model", "stage", "direction"],  # direction: input | output
)
LLM_COST = Counter("llmapp_llm_cost_usd_total", "Estimated LLM spend (USD, reference pricing)", ["model", "stage"])
LLM_TTFT = Histogram(
    "llmapp_llm_ttft_seconds", "Client-side time to first token", ["model", "stage"], buckets=TTFT_BUCKETS
)
LLM_DURATION = Histogram(
    "llmapp_llm_call_duration_seconds", "Client-side LLM call duration", ["model", "stage"], buckets=STAGE_BUCKETS
)


def exemplar() -> dict[str, str] | None:
    """Exemplar labels linking a histogram sample to the active trace."""
    trace_id, _ = current_ids()
    return {"trace_id": trace_id} if trace_id else None


def observe(hist: Histogram, value: float, **labels: str) -> None:
    child = hist.labels(**labels) if labels else hist
    child.observe(value, exemplar=exemplar())


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    return input_tokens * PRICE_IN_PER_M / 1e6 + output_tokens * PRICE_OUT_PER_M / 1e6
