"""Tracing + structured-logging bootstrap.

Three pillars, wired together:

* **Traces**  - OpenTelemetry SDK -> OTLP/gRPC -> OTel Collector -> Tempo.
* **Logs**    - one JSON object per line; every line carries the active ``trace_id`` / ``span_id`` so Grafana can
                jump from a log line to the trace (and back).
* **Metrics** - live in ``llmserve.obs.metrics`` (prometheus_client); histograms attach the ``trace_id`` as an
                *exemplar* so a latency spike on a graph links to a trace.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "llm-gateway-demo")
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "0.1.0")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

# LogRecord attributes that are *not* user-supplied ``extra`` fields.
_STD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def current_ids() -> tuple[str, str]:
    """Return (trace_id, span_id) of the active span as hex strings ('' if none)."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return "", ""
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def setup_tracing(exporter: object | None = None) -> TracerProvider:  # pragma: no cover - needs a collector
    """Install a global TracerProvider that batches spans to the OTLP collector (or to ``exporter``)."""
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
            "deployment.environment.name": os.getenv("DEPLOY_ENV", "lab"),
            "host.name": os.getenv("HOSTNAME", "gpu-host"),
        }
    )
    provider = TracerProvider(resource=resource)  # default sampler = ParentBased(ALWAYS_ON)
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter or OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True),
            max_export_batch_size=256,
            schedule_delay_millis=1000,
        )
    )
    trace.set_tracer_provider(provider)
    return provider


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with trace correlation fields."""

    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id = current_ids()
        doc: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": SERVICE_NAME,
            "trace_id": trace_id,
            "span_id": span_id,
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                doc[key] = value
        if record.exc_info:
            doc["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            doc["exc"] = self.formatException(record.exc_info)[-1500:]
        return json.dumps(doc, default=str, ensure_ascii=False)


def setup_logging(log_file: str | None = None, level: str = "INFO") -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    fmt = JsonFormatter()
    for h in handlers:
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)
    # httpx logs one INFO line per request; the spans already carry that, keep logs readable.
    for noisy in ("httpx", "httpcore", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
