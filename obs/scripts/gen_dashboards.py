#!/usr/bin/env python3
"""Generate the three provisioned Grafana dashboards (JSON) from compact Python.

Why generate? Dashboard JSON is 90% boilerplate; the *queries* are the knowledge. Keeping them in
code makes review/diff sane. Output: obs/grafana/dashboards/{llm-serving,app,infra}.json
Method tags in panel titles: RED = Rate/Errors/Duration (services), USE = Utilization/Saturation/
Errors (resources). Every panel's description says which signal it is and what to do about it.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
TEMPO = {"type": "tempo", "uid": "tempo"}
LOKI = {"type": "loki", "uid": "loki"}
RI = "$__rate_interval"

GREEN, YELLOW, RED, BLUE = "green", "yellow", "red", "blue"


def steps(*pairs):
    """steps((None,'green'),(2,'red')) -> grafana threshold steps."""
    return [{"color": c, "value": v} for v, c in pairs]


def tgt(expr, legend="", ref="A", instant=False, exemplar=False):
    return {
        "refId": ref,
        "datasource": PROM,
        "expr": expr,
        "legendFormat": legend,
        "instant": instant,
        "range": not instant,
        "exemplar": exemplar,
    }


class Board:
    def __init__(self, uid, title, desc, tags, variables=None, alert_re=".+"):
        self.uid, self.title, self.desc, self.tags = uid, title, desc, tags
        self.alert_re = alert_re
        self.variables = variables or []
        self.panels: list[dict] = []
        self._id = 0
        self._y = 0
        self._x = 0
        self._rowh = 0

    def _place(self, w, h):
        if self._x + w > 24:
            self._y += self._rowh
            self._x, self._rowh = 0, 0
        pos = {"x": self._x, "y": self._y, "w": w, "h": h}
        self._x += w
        self._rowh = max(self._rowh, h)
        return pos

    def _add(self, panel, w, h):
        self._id += 1
        panel.update({"id": self._id, "gridPos": self._place(w, h)})
        self.panels.append(panel)

    def row(self, title):
        self._y += self._rowh
        self._x, self._rowh = 0, 0
        self._id += 1
        self.panels.append(
            {
                "id": self._id,
                "type": "row",
                "title": title,
                "collapsed": False,
                "gridPos": {"x": 0, "y": self._y, "w": 24, "h": 1},
                "panels": [],
            }
        )
        self._y += 1

    def text(self, md, w=24, h=4):
        self._add({"type": "text", "title": "", "options": {"mode": "markdown", "content": md}}, w, h)

    def ts(
        self,
        title,
        targets,
        w=8,
        h=8,
        unit="short",
        desc="",
        stack=False,
        thr=None,
        vmin=None,
        vmax=None,
        legend="list",
        fill=12,
        thr_style="off",
        overrides=None,
    ):
        d = {
            "unit": unit,
            "custom": {
                "drawStyle": "line",
                "lineWidth": 1,
                "fillOpacity": fill,
                "showPoints": "never",
                "spanNulls": True,
                "stacking": {"mode": "normal" if stack else "none"},
                "thresholdsStyle": {"mode": thr_style},
            },
            "thresholds": {"mode": "absolute", "steps": thr or steps((None, GREEN))},
        }
        if vmin is not None:
            d["min"] = vmin
        if vmax is not None:
            d["max"] = vmax
        self._add(
            {
                "type": "timeseries",
                "title": title,
                "description": desc,
                "datasource": PROM,
                "targets": targets,
                "fieldConfig": {"defaults": d, "overrides": overrides or []},
                "options": {
                    "legend": {
                        "displayMode": "table" if legend == "table" else "list",
                        "placement": "bottom",
                        "calcs": ["mean", "max"] if legend == "table" else [],
                    },
                    "tooltip": {"mode": "multi", "sort": "desc"},
                },
            },
            w,
            h,
        )

    def stat(
        self, title, expr, w=4, h=4, unit="short", thr=None, desc="", decimals=None, kind="stat", vmin=None, vmax=None
    ):
        d = {
            "unit": unit,
            "thresholds": {"mode": "absolute", "steps": thr or steps((None, GREEN))},
            "color": {"mode": "thresholds"},
        }
        if decimals is not None:
            d["decimals"] = decimals
        if vmin is not None:
            d["min"] = vmin
        if vmax is not None:
            d["max"] = vmax
        opts = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}
        if kind == "stat":
            opts.update({"colorMode": "background", "graphMode": "area", "textMode": "auto", "justifyMode": "auto"})
        else:
            opts.update({"showThresholdLabels": False, "showThresholdMarkers": True})
        self._add(
            {
                "type": kind,
                "title": title,
                "description": desc,
                "datasource": PROM,
                "targets": [tgt(expr, instant=False)],
                "fieldConfig": {"defaults": d, "overrides": []},
                "options": opts,
            },
            w,
            h,
        )

    def table(self, title, targets, w=12, h=8, desc=""):
        self._add(
            {
                "type": "table",
                "title": title,
                "description": desc,
                "datasource": PROM,
                "targets": targets,
                "fieldConfig": {"defaults": {}, "overrides": []},
                "options": {"showHeader": True},
                "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True}}}],
            },
            w,
            h,
        )

    def up_table(self, title, w=12, h=9):
        """`up` per target as a clean UP/DOWN table (value-mapped)."""
        self._add(
            {
                "type": "table",
                "title": title,
                "description": "Every Prometheus target. vllm-extra is expected to be DOWN when its server is stopped "
                "(optional=true, exempt from TargetDown).",
                "datasource": PROM,
                "targets": [{**tgt("up", "", "A", instant=True), "format": "table"}],
                "fieldConfig": {
                    "defaults": {},
                    "overrides": [
                        {
                            "matcher": {"id": "byName", "options": "state"},
                            "properties": [
                                {
                                    "id": "mappings",
                                    "value": [
                                        {
                                            "type": "value",
                                            "options": {
                                                "0": {"text": "DOWN", "color": "red", "index": 0},
                                                "1": {"text": "UP", "color": "green", "index": 1},
                                            },
                                        }
                                    ],
                                },
                                {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                            ],
                        }
                    ],
                },
                "options": {"showHeader": True, "sortBy": [{"displayName": "state", "desc": False}]},
                "transformations": [
                    {
                        "id": "organize",
                        "options": {
                            "excludeByName": {
                                "Time": True,
                                "__name__": True,
                                "engine": True,
                                "optional": True,
                                "namespace": True,
                                "cluster": True,
                            },
                            "renameByName": {"Value": "state"},
                            "indexByName": {"job": 0, "instance": 1, "state": 2},
                        },
                    }
                ],
            },
            w,
            h,
        )

    def traces(self, title, query, w=12, h=8, desc=""):
        self._add(
            {
                "type": "table",
                "title": title,
                "description": desc,
                "datasource": TEMPO,
                "targets": [
                    {
                        "refId": "A",
                        "datasource": TEMPO,
                        "queryType": "traceql",
                        "query": query,
                        "limit": 15,
                        "tableType": "traces",
                    }
                ],
                "fieldConfig": {"defaults": {}, "overrides": []},
                "options": {"showHeader": True},
            },
            w,
            h,
        )

    def logs(self, title, expr, w=24, h=9, desc=""):
        self._add(
            {
                "type": "logs",
                "title": title,
                "description": desc,
                "datasource": LOKI,
                "targets": [{"refId": "A", "datasource": LOKI, "expr": expr, "queryType": "range"}],
                "options": {
                    "showTime": True,
                    "wrapLogMessage": True,
                    "enableLogDetails": True,
                    "sortOrder": "Descending",
                    "dedupStrategy": "none",
                },
            },
            w,
            h,
        )

    def json(self):
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.desc,
            "tags": self.tags,
            "schemaVersion": 41,
            "version": 1,
            "editable": True,
            "graphTooltip": 1,
            "time": {"from": "now-30m", "to": "now"},
            "refresh": "10s",
            "timezone": "browser",
            "templating": {"list": self.variables},
            "annotations": {
                "list": [
                    {
                        "builtIn": 1,
                        "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                        "enable": True,
                        "hide": True,
                        "iconColor": "rgba(0, 211, 255, 1)",
                        "name": "Annotations & Alerts",
                        "type": "dashboard",
                    },
                    {
                        "name": "Alerts firing",
                        "datasource": PROM,
                        "enable": True,
                        "iconColor": "red",
                        "expr": f'ALERTS{{alertstate="firing",alertname=~"{self.alert_re}"}}',
                        "titleFormat": "{{alertname}}",
                        "textFormat": "{{summary}}",
                        "step": "15s",
                    },
                    {
                        "name": "Fault windows",
                        "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                        "enable": True,
                        "iconColor": "orange",
                        "target": {"type": "tags", "tags": ["fault"], "limit": 100, "matchAny": False},
                    },
                ]
            },
            "links": [
                {"title": "LLM serving", "type": "link", "url": "/d/llm-serving", "icon": "dashboard"},
                {"title": "App", "type": "link", "url": "/d/app", "icon": "dashboard"},
                {"title": "Infra", "type": "link", "url": "/d/infra", "icon": "dashboard"},
            ],
            "panels": self.panels,
        }

    def write(self):
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{self.uid}.json").write_text(json.dumps(self.json(), indent=1) + "\n")
        print("wrote", self.uid, f"({len(self.panels)} panels)")


def q(metric, f, p, by="le"):
    """histogram_quantile helper over a classic histogram."""
    return f"histogram_quantile({p}, sum by ({by}) (rate({metric}_bucket{{{f}}}[{RI}])))"


def avg(metric, f, by=""):
    g = f"by ({by}) " if by else ""
    return f"sum {g}(rate({metric}_sum{{{f}}}[{RI}])) / sum {g}(rate({metric}_count{{{f}}}[{RI}]))"


# ============================================================================== LLM serving
def llm_serving():
    inst = {
        "name": "instance",
        "label": "vLLM instance",
        "type": "query",
        "datasource": PROM,
        "query": {"query": "label_values(vllm:num_requests_running, instance)", "refId": "v"},
        "refresh": 2,
        "includeAll": False,
        "multi": False,
        "current": {"text": "obs-vllm", "value": "obs-vllm"},
    }
    b = Board(
        "llm-serving",
        "LLM serving (vLLM)",
        "Golden signals and saturation of the model servers. RED = request-centric (Rate, Errors, Duration); "
        "USE = resource-centric (Utilization, Saturation, Errors).",
        ["llm", "vllm", "red", "use"],
        [inst],
        alert_re="LLM.*",
    )
    f = 'instance=~"$instance"'
    b.text(
        "**How to read this board.** *Top row = is it OK right now?* Then **RED** rows describe the experience of requests "
        "(how many, how many fail, how long) and the **USE** row describes the resource (KV cache = the scarce resource, "
        "queue = saturation). Latency is split the way LLM users feel it: **TTFT** (wait before the first token) vs "
        "**inter-token latency / TPOT** (speed of the stream) vs **E2E**. "
        "Select the engine with the `instance` variable (obs-vllm = the container started by run_obs_vllm.sh, vllm-extra = an optional second server when up).",
        h=4,
    )

    b.row("Now (stat row)")
    b.stat(
        "Running requests",
        f"sum(vllm:num_requests_running{{{f}}})",
        desc="USE: requests currently in the engine batch.",
    )
    b.stat(
        "Waiting requests",
        f"sum(vllm:num_requests_waiting{{{f}}})",
        thr=steps((None, GREEN), (1, YELLOW), (5, RED)),
        desc="USE-Saturation: requests admitted but not yet scheduled. >0 means demand > capacity. Alert: LLMRequestQueueGrowing.",
    )
    b.stat(
        "KV cache usage",
        f"max(vllm:kv_cache_usage_perc{{{f}}})",
        unit="percentunit",
        kind="gauge",
        vmin=0,
        vmax=1,
        thr=steps((None, GREEN), (0.7, YELLOW), (0.9, RED)),
        desc="USE-Utilization of THE scarce resource. At 100% requests queue or are preempted. Alert at 90%.",
    )
    b.stat(
        "TTFT p95 (2m)",
        f"histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{{{f}}}[2m])))",
        unit="s",
        thr=steps((None, GREEN), (1, YELLOW), (2, RED)),
        desc="RED-Duration, user-facing. SLO 2s. Alert: LLMTimeToFirstTokenHigh.",
    )
    b.stat(
        "Generation tokens/s",
        f"sum(rate(vllm:generation_tokens_total{{{f}}}[1m]))",
        unit="short",
        desc="Throughput: output tokens per second across all requests.",
    )
    b.stat(
        "Prefix-cache hit rate (5m)",
        f"sum(rate(vllm:prefix_cache_hits_total{{{f}}}[5m])) / sum(rate(vllm:prefix_cache_queries_total{{{f}}}[5m]))",
        unit="percentunit",
        thr=steps((None, RED), (0.3, YELLOW), (0.6, GREEN)),
        desc="Share of prompt tokens served from cached KV blocks. A long shared system prompt means this should be high.",
    )

    b.row("RED - Duration: how long do users wait?")
    b.ts(
        "Time to first token (TTFT)",
        [
            tgt(q("vllm:time_to_first_token_seconds", f, 0.5), "p50", "A"),
            tgt(q("vllm:time_to_first_token_seconds", f, 0.95), "p95", "B"),
            tgt(q("vllm:time_to_first_token_seconds", f, 0.99), "p99", "C"),
        ],
        w=8,
        unit="s",
        thr=steps((None, GREEN), (2, RED)),
        thr_style="line",
        legend="table",
        desc="Queue wait + prefill (processing the prompt) + first decode step. Grows with queue depth and prompt length. "
        "Percentiles are estimated from histogram buckets (linear interpolation inside a bucket) - approximate, see README.",
    )
    b.ts(
        "Inter-token latency (TPOT)",
        [
            tgt(q("vllm:inter_token_latency_seconds", f, 0.5), "inter-token p50", "A"),
            tgt(q("vllm:inter_token_latency_seconds", f, 0.95), "inter-token p95", "B"),
            tgt(q("vllm:request_time_per_output_token_seconds", f, 0.95), "per-request avg TPOT p95", "C"),
        ],
        w=8,
        unit="s",
        legend="table",
        desc="Gap between streamed tokens = how fast the answer 'types'. Decode is memory-bandwidth bound, so it degrades with batch size and when the GPU is shared. 1/TPOT = tokens/s per user.",
    )
    b.ts(
        "End-to-end request latency",
        [
            tgt(q("vllm:e2e_request_latency_seconds", f, 0.5), "p50", "A"),
            tgt(q("vllm:e2e_request_latency_seconds", f, 0.95), "p95", "B"),
            tgt(q("vllm:e2e_request_latency_seconds", f, 0.99), "p99", "C"),
        ],
        w=8,
        unit="s",
        legend="table",
        desc="Queue + prefill + decode, server side. Compare with the app's client-side view in the App dashboard.",
    )
    b.ts(
        "Where does a request spend its time? (mean, stacked)",
        [
            tgt(avg("vllm:request_queue_time_seconds", f), "queue", "A"),
            tgt(avg("vllm:request_prefill_time_seconds", f), "prefill", "B"),
            tgt(avg("vllm:request_decode_time_seconds", f), "decode", "C"),
        ],
        w=12,
        unit="s",
        stack=True,
        desc="Queue = waiting for a free slot / KV blocks (saturation). Prefill = prompt processing (compute-bound, scales with prompt length). Decode = generation (bandwidth-bound, scales with output length).",
    )
    b.ts(
        "Prompt vs generation length per request (mean)",
        [
            tgt(avg("vllm:request_prompt_tokens", f), "prompt tokens", "A"),
            tgt(avg("vllm:request_generation_tokens", f), "generation tokens", "B"),
        ],
        w=12,
        unit="short",
        desc="Workload shape. Schema-in-prompt text-to-SQL and RAG are prompt-heavy and output-light, so prefix caching matters more than decode speed.",
    )

    b.row("RED - Rate & Errors: how much work, how much fails?")
    b.ts(
        "Requests finished per second, by reason",
        [tgt(f"sum by (finished_reason) (rate(vllm:request_success_total{{{f}}}[{RI}]))", "{{finished_reason}}")],
        w=8,
        unit="reqps",
        stack=True,
        desc="stop = natural end, length = hit max_tokens (truncated answer - a QUALITY signal), abort = client went away (timeouts upstream show up here), error = engine failure.",
    )
    b.ts(
        "Token throughput",
        [
            tgt(f"sum(rate(vllm:prompt_tokens_total{{{f}}}[{RI}]))", "prompt tokens/s (prefill)", "A"),
            tgt(f"sum(rate(vllm:generation_tokens_total{{{f}}}[{RI}]))", "generation tokens/s (decode)", "B"),
        ],
        w=8,
        unit="short",
        desc="Capacity actually delivered. Prefill and decode have very different costs per token, so watch both.",
    )
    b.ts(
        "Tokens per engine step (effective batch size)",
        [tgt(avg("vllm:iteration_tokens_total", f), "tokens / step")],
        w=8,
        unit="short",
        desc="Average tokens processed per scheduler iteration = how well continuous batching is filling the GPU.",
    )

    b.row("USE - Saturation: is the scarce resource running out?")
    b.ts(
        "Running vs waiting requests",
        [
            tgt(f"sum(vllm:num_requests_running{{{f}}})", "running", "A"),
            tgt(f"sum(vllm:num_requests_waiting{{{f}}})", "waiting", "B"),
        ],
        w=8,
        unit="short",
        overrides=[
            {
                "matcher": {"id": "byName", "options": "waiting"},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}],
            }
        ],
        desc="Waiting > 0 = saturation (USE). The line you want flat at 0. Alert: LLMRequestQueueGrowing (avg >4 for 1m and not draining).",
    )
    b.ts(
        "KV cache usage",
        [tgt(f"max by (instance) (vllm:kv_cache_usage_perc{{{f}}})", "{{instance}}")],
        w=8,
        unit="percentunit",
        vmin=0,
        vmax=1,
        thr=steps((None, GREEN), (0.9, RED)),
        thr_style="line",
        desc="Fraction of KV-cache blocks in use. Each running token holds K/V for every layer; this bounds concurrency x context length. >90% alert line.",
    )
    b.ts(
        "Preemptions per second",
        [tgt(f"sum(rate(vllm:num_preemptions_total{{{f}}}[{RI}]))", "preemptions/s")],
        w=8,
        unit="ops",
        thr=steps((None, GREEN), (0.0001, RED)),
        thr_style="line",
        desc="When KV runs out mid-generation vLLM evicts a running request and recomputes it later. Any non-zero value = wasted work + latency tail. Alert: LLMPreemptionsDetected.",
    )
    b.ts(
        "Prefix-cache hit rate",
        [
            tgt(
                f"sum(rate(vllm:prefix_cache_hits_total{{{f}}}[{RI}])) / sum(rate(vllm:prefix_cache_queries_total{{{f}}}[{RI}]))",
                "hit rate",
            )
        ],
        w=8,
        unit="percentunit",
        vmin=0,
        vmax=1,
        desc="Hit rate of the automatic prefix cache (token-level). Requests with the same leading tokens skip recomputing their prefill.",
    )
    b.ts(
        "Prompt tokens by source",
        [tgt(f"sum by (source) (rate(vllm:prompt_tokens_by_source_total{{{f}}}[{RI}]))", "{{source}}")],
        w=8,
        unit="short",
        stack=True,
        desc="Where prompt tokens came from: computed locally vs served from the (local) prefix cache vs external caches. Savings = cached share.",
    )
    b.ts(
        "Engine/GPU context (shared box!)",
        [tgt("DCGM_FI_DEV_GPU_UTIL", "GPU util % (whole GPU, all tenants)", "A")],
        w=8,
        unit="percent",
        vmin=0,
        vmax=100,
        desc="On a shared GPU our decode speed is bounded by what the other tenants leave us. Use it to explain a slow TPOT.",
    )

    return b


# ============================================================================== Demo service
def app_board():
    b = Board(
        "app",
        "App (RED) - LLM-backed demo service",
        "RED method for the FastAPI demo service: Rate, Errors, Duration, plus stage latency, tokens and cost.",
        ["app", "red", "slo", "genai"],
        alert_re="App.*",
    )
    ask = 'route="/ask"'
    b.text(
        "**RED** = *Rate* (requests/s), *Errors* (5xx share - the SLI), *Duration* (latency). The SLO is **99% of /ask requests succeed** "
        "(error budget 1%). *Burn rate* = error ratio / 0.01: 1 means spending the budget exactly on schedule, 14.4 means it is gone in 2 days. "
        "The small dots on the latency graph are **exemplars**: click one to open the trace of a request that landed in that latency bucket. "
        "Bottom row: traces and logs from the same requests, joined by `trace_id`.",
        h=4,
    )

    b.row("Golden signals (RED) - right now")
    b.stat(
        "Request rate",
        f"sum(rate(llmapp_http_requests_total{{{ask}}}[1m]))",
        unit="reqps",
        decimals=2,
        desc="RED-Rate.",
    )
    b.stat(
        "Error ratio (5m)",
        "app:ask_error_ratio:rate5m",
        unit="percentunit",
        thr=steps((None, GREEN), (0.01, YELLOW), (0.144, RED)),
        decimals=1,
        desc="RED-Errors / the SLI. Yellow = above the 1% budget rate, red = fast-burn threshold (14.4x).",
    )
    b.stat(
        "Burn rate (1h)",
        "app:ask_error_ratio:rate1h / 0.01",
        unit="none",
        thr=steps((None, GREEN), (1, YELLOW), (6, "orange"), (14.4, RED)),
        decimals=1,
        desc="1h error ratio divided by the budget (0.01). Page when >14.4 on both 1h and 5m windows (AppSLOBurnRateFast).",
    )
    b.stat(
        "Latency p95 (5m)",
        "app:ask_latency_seconds:p95_5m",
        unit="s",
        thr=steps((None, GREEN), (20, YELLOW), (30, RED)),
        desc="RED-Duration. The stage panels show where the time goes.",
    )
    b.stat(
        "In flight", "llmapp_http_inflight_requests", unit="short", desc="Concurrency = rate x latency (Little's law)."
    )

    b.row("RED - Rate, Errors, Duration over time")
    b.ts(
        "Requests/s by HTTP status",
        [tgt(f"sum by (status) (rate(llmapp_http_requests_total{{{ask}}}[{RI}]))", "{{status}}")],
        w=8,
        unit="reqps",
        stack=True,
        desc="RED-Rate + Errors. 200 = success; 502/504 = LLM backend error / timeout.",
    )
    b.ts(
        "Error ratio vs SLO",
        [
            tgt("app:ask_error_ratio:rate5m", "5m", "A"),
            tgt("app:ask_error_ratio:rate1h", "1h", "B"),
            tgt("app:ask_error_ratio:rate6h", "6h", "C"),
        ],
        w=8,
        unit="percentunit",
        vmin=0,
        vmax=1,
        thr=steps((None, GREEN), (0.01, YELLOW), (0.144, RED)),
        thr_style="line",
        desc="Multi-window view behind the burn-rate alerts. Dashed lines: 1% (budget rate) and 14.4% (fast-burn threshold = 14.4 x 1%). Short window reacts fast, long window filters blips.",
    )
    b.ts(
        "Latency (end to end)",
        [
            tgt(q("llmapp_http_request_duration_seconds", ask, 0.5), "p50", "A"),
            tgt(q("llmapp_http_request_duration_seconds", ask, 0.95), "p95", "B", exemplar=True),
            tgt(q("llmapp_http_request_duration_seconds", ask, 0.99), "p99", "C"),
        ],
        w=8,
        unit="s",
        legend="table",
        desc="RED-Duration. Dots on the p95 line are exemplars (trace ids): click -> Tempo. Percentiles from histogram buckets are approximate.",
    )

    b.row("Where does the time go? (stages)")
    b.ts(
        "Stage latency p95",
        [tgt(q("llmapp_stage_duration_seconds", "", 0.95, by="le, stage"), "{{stage}}")],
        w=8,
        unit="s",
        desc="p95 per stage of the pipeline: answer (LLM call #1) and summarize (LLM call #2).",
    )
    b.ts(
        "Mean time per stage (stacked)",
        [tgt(avg("llmapp_stage_duration_seconds", "", by="stage"), "{{stage}}")],
        w=8,
        unit="s",
        stack=True,
        desc="Stacked means sum to roughly the mean request latency: the biggest slice is the bottleneck.",
    )
    b.ts(
        "Client-side TTFT & call duration (LLM)",
        [
            tgt(q("llmapp_llm_ttft_seconds", "", 0.95, by="le, stage"), "TTFT p95 {{stage}}", "A"),
            tgt(q("llmapp_llm_call_duration_seconds", "", 0.95, by="le, stage"), "call p95 {{stage}}", "B"),
        ],
        w=8,
        unit="s",
        desc="What the app measured by streaming. Compare with vLLM's server-side TTFT: the gap is network + app event loop + client queueing.",
    )

    b.row("Tokens and cost (estimated, reference pricing: $0.10 / $0.40 per M tokens in / out)")
    b.ts(
        "Tokens per second",
        [tgt("sum by (direction) (rate(llmapp_llm_tokens_total[" + RI + "]))", "{{direction}}")],
        w=6,
        unit="short",
        stack=True,
        desc="Input vs output token flow of the service (all stages).",
    )
    b.ts(
        "Tokens per answered request",
        [
            tgt(
                "sum by (direction) (rate(llmapp_llm_tokens_total["
                + RI
                + '])) / ignoring(direction) group_left sum(rate(llmapp_requests_total{outcome="ok"}['
                + RI
                + "]))",
                "{{direction}}",
            )
        ],
        w=6,
        unit="short",
        desc="Cost driver. A sudden rise = prompt bloat or retry loops.",
    )
    b.ts(
        "Estimated cost per answered request",
        [
            tgt(
                "sum(rate(llmapp_llm_cost_usd_total["
                + RI
                + '])) / sum(rate(llmapp_requests_total{outcome="ok"}['
                + RI
                + "]))",
                "USD / request",
            )
        ],
        w=6,
        unit="currencyUSD",
        desc="Includes tokens wasted on failed requests (numerator counts all LLM spend).",
    )
    b.ts(
        "LLM finish reasons",
        [
            tgt(
                'sum by (finish_reason) (rate(llmapp_llm_requests_total{outcome="ok"}[' + RI + "]))",
                "{{finish_reason}}",
            )
        ],
        w=6,
        unit="reqps",
        stack=True,
        desc="'length' = answer truncated at max_tokens. Rising share = quality regression hiding behind 200s.",
    )

    b.row("Failure modes")
    b.ts(
        "LLM call failures by type",
        [tgt('sum by (outcome) (rate(llmapp_llm_requests_total{outcome!="ok"}[' + RI + "]))", "{{outcome}}")],
        w=12,
        unit="ops",
        stack=True,
        desc="timeout / connect_error / http_5xx. Each failed attempt is retried once, so one bad request shows as 2 failures.",
    )
    b.ts(
        "Answers by outcome",
        [tgt("sum by (outcome) (rate(llmapp_requests_total[" + RI + "]))", "{{outcome}}")],
        w=12,
        unit="reqps",
        stack=True,
        desc="ok vs error at the application level (the HTTP view is the first row).",
    )

    b.row("Derived from traces (OTel collector spanmetrics) - same RED, different source")
    b.ts(
        "Span rate by operation (llm-gateway-demo)",
        [
            tgt(
                'sum by (span_name) (rate(traces_span_metrics_calls_total{service_name="llm-gateway-demo"}['
                + RI
                + "]))",
                "{{span_name}}",
            )
        ],
        w=12,
        unit="ops",
        desc="Rates computed from spans by the collector, not by the app: proves the pipeline end to end and gives RED for services you did not instrument with metrics.",
    )
    b.ts(
        "Span p95 by operation (traces)",
        [
            tgt(
                'histogram_quantile(0.95, sum by (le, span_name) (rate(traces_span_metrics_duration_milliseconds_bucket{service_name="llm-gateway-demo",span_name=~"invoke_agent.*|chat.*|agent.*"}['
                + RI
                + "]))) / 1000",
                "{{span_name}}",
            )
        ],
        w=12,
        unit="s",
        desc="Latency by span type derived from traces.",
    )

    b.row("Traces and logs (same requests, linked by trace_id)")
    b.traces(
        "Recent ERROR traces",
        '{resource.service.name="llm-gateway-demo" && status=error && name="POST /ask"}',
        w=12,
        desc="TraceQL. Click a TraceID to open the waterfall; the trace view has 'Logs for this span' and 'Request rate' links.",
    )
    b.traces(
        "Slowest recent /ask traces (>20s)",
        '{resource.service.name="llm-gateway-demo" && name="POST /ask" && duration > 20s}',
        w=12,
    )
    b.logs(
        "Warnings and errors (JSON logs with trace_id; click 'Open trace')",
        '{service_name="llm-gateway-demo"} | json | level=~"WARNING|ERROR"',
    )
    return b


# ============================================================================== Infra
def infra():
    b = Board(
        "infra",
        "Infra - host, GPU, pipeline",
        "USE method (Utilization, Saturation, Errors) for the host, the GPU and the observability pipeline itself.",
        ["infra", "use", "gpu"],
        alert_re="TargetDown|HostMemoryLow|GPU.*|OTel.*",
    )
    b.text(
        "**USE** per resource: *Utilization* (how busy), *Saturation* (work queued/waiting), *Errors*. "
        "GB10 note: CPU and GPU share one **unified memory** pool, so 'GPU memory' = system memory pressure; per-process GPU memory comes from the small nvidia-smi exporter (DCGM reports no framebuffer / per-process memory on GB10). "
        "The bottom row monitors the monitoring (collector, Prometheus).",
        h=4,
    )
    b.row("Scrape health")
    b.up_table("Scrape targets", w=12, h=9)
    b.stat(
        "GPU utilization",
        "DCGM_FI_DEV_GPU_UTIL",
        unit="percent",
        kind="gauge",
        vmin=0,
        vmax=100,
        w=4,
        h=9,
        thr=steps((None, GREEN), (80, YELLOW), (95, RED)),
        desc="USE-Utilization. Whole GPU, all tenants.",
    )
    b.stat(
        "Memory available",
        "node_memory_MemAvailable_bytes",
        unit="bytes",
        w=4,
        h=9,
        thr=steps((None, RED), (6e9, YELLOW), (12e9, GREEN)),
        desc="Unified memory left for everyone (CPU + GPU). Alert: HostMemoryLow < 6 GB.",
    )
    b.stat("GPU power", "DCGM_FI_DEV_POWER_USAGE", unit="watt", w=4, h=9, desc="Power draw (W).")

    b.row("GPU (DCGM exporter for util/power/temp; nvidia-smi exporter for per-workload memory)")
    b.ts(
        "GPU utilization & power",
        [tgt("DCGM_FI_DEV_GPU_UTIL", "util %", "A"), tgt("DCGM_FI_DEV_POWER_USAGE", "power W", "B")],
        w=8,
        unit="short",
        desc="USE-Utilization. 100% util with few tokens/s means someone else is hogging it.",
    )
    b.ts(
        "GPU memory by workload (unified memory pool)",
        [tgt("gpu_process_memory_bytes", "{{workload}}")],
        w=8,
        unit="bytes",
        stack=True,
        desc="Who holds the memory. Each model server pins its weights + KV: this is the 'capacity planning' view for a shared box.",
    )
    b.ts(
        "GPU temperature & SM clock",
        [tgt("DCGM_FI_DEV_GPU_TEMP", "temp C", "A"), tgt("DCGM_FI_DEV_SM_CLOCK / 100", "SM clock (x100 MHz)", "B")],
        w=8,
        unit="short",
        desc="Thermal throttling shows as clock drops at high temp.",
    )

    b.row("CPU, memory, disk, network (node-exporter)")
    b.ts(
        "CPU utilization (USE-U) and load per core (USE-S)",
        [
            tgt('1 - avg(rate(node_cpu_seconds_total{mode="idle"}[' + RI + "]))", "CPU busy", "A"),
            tgt("node_load1 / count(count by (cpu) (node_cpu_seconds_total))", "load1 per core (saturation)", "B"),
        ],
        w=8,
        unit="percentunit",
        desc="Utilization = share of non-idle time. Load per core > 1 means runnable threads are waiting = saturation.",
    )
    b.ts(
        "Memory (unified)",
        [
            tgt("node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes", "used", "A"),
            tgt("node_memory_MemAvailable_bytes", "available", "B"),
            tgt("node_memory_Cached_bytes", "page cache", "C"),
        ],
        w=8,
        unit="bytes",
        desc="USE-Utilization of memory. 'Available' counts reclaimable cache; memory-mapped model weights (e.g. llama.cpp) live in the page cache.",
    )
    b.ts(
        "Memory pressure (PSI) & swap",
        [
            tgt("rate(node_pressure_memory_waiting_seconds_total[" + RI + "])", "PSI memory some (s/s)", "A"),
            tgt("rate(node_vmstat_pswpout[" + RI + "])", "swap-out pages/s", "B"),
        ],
        w=8,
        unit="short",
        desc="USE-Saturation of memory: time tasks stall waiting for memory, and swapping. Non-zero PSI = the box is memory-starved.",
    )
    b.ts(
        "Disk busy",
        [tgt('rate(node_disk_io_time_seconds_total{device=~"nvme.*"}[' + RI + "])", "{{device}}")],
        w=8,
        unit="percentunit",
        desc="USE-Utilization of the NVMe.",
    )
    b.ts(
        "Network",
        [
            tgt(
                'sum(rate(node_network_receive_bytes_total{device!~"lo|veth.*|br-.*|docker.*"}[' + RI + "]))", "rx", "A"
            ),
            tgt(
                'sum(rate(node_network_transmit_bytes_total{device!~"lo|veth.*|br-.*|docker.*"}[' + RI + "]))",
                "tx",
                "B",
            ),
        ],
        w=8,
        unit="Bps",
    )
    b.ts("Filesystem free (/)", [tgt('node_filesystem_avail_bytes{mountpoint="/"}', "free")], w=8, unit="bytes")

    b.row("Monitoring the monitoring (pipeline health)")
    b.ts(
        "OTel collector: spans accepted / sent / refused",
        [
            tgt("sum(rate(otelcol_receiver_accepted_spans__spans__total[" + RI + "]))", "accepted", "A"),
            tgt("sum(rate(otelcol_exporter_sent_spans__spans__total[" + RI + "]))", "sent (all exporters)", "B"),
            tgt("sum(rate(otelcol_receiver_refused_spans__spans__total[" + RI + "]))", "refused", "C"),
            tgt("sum(rate(otelcol_exporter_send_failed_spans__spans__total[" + RI + "]))", "send failed", "D"),
        ],
        w=8,
        unit="ops",
        desc="If refused/failed > 0 traces are being LOST - alert OTelCollectorDroppingTelemetry.",
    )
    b.ts(
        "Prometheus: active series & samples/s",
        [
            tgt("prometheus_tsdb_head_series", "head series", "A"),
            tgt("rate(prometheus_tsdb_head_samples_appended_total[" + RI + "])", "samples/s", "B"),
        ],
        w=8,
        unit="short",
        desc="Cardinality budget. Series count is what blows up Prometheus memory.",
    )
    b.ts(
        "Scrape duration by job (s)",
        [tgt("scrape_duration_seconds", "{{job}}/{{instance}}")],
        w=8,
        unit="s",
        desc="Slow scrapes can approach the scrape interval and cause gaps.",
    )
    return b


if __name__ == "__main__":
    for board in (llm_serving(), app_board(), infra()):
        board.write()
