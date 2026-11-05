#!/usr/bin/env python3
"""Export the numbers behind a scenario (what the dashboards show) as plain text.

  evidence_dump.py <name> <start_epoch> <end_epoch> [llm|app|infra|all]
Writes obs/evidence/metrics/<name>.txt : for each PromQL, per series min / mean / max / last and a
sparkline-ish list of ~12 evenly spaced samples. Works even if screenshots are unavailable.
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PROM = "http://127.0.0.1:9090"
OUT = Path(__file__).resolve().parent.parent / "evidence" / "metrics"

SETS = {
    "llm": [
        (
            "TTFT p95 (s), vLLM server-side",
            'histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{instance="obs-vllm"}[1m])))',
        ),
        (
            "TTFT p50 (s)",
            'histogram_quantile(0.5, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket{instance="obs-vllm"}[1m])))',
        ),
        (
            "inter-token latency p95 (s)",
            'histogram_quantile(0.95, sum by (le) (rate(vllm:inter_token_latency_seconds_bucket{instance="obs-vllm"}[1m])))',
        ),
        (
            "e2e latency p95 (s)",
            'histogram_quantile(0.95, sum by (le) (rate(vllm:e2e_request_latency_seconds_bucket{instance="obs-vllm"}[1m])))',
        ),
        (
            "queue time mean (s)",
            'sum(rate(vllm:request_queue_time_seconds_sum{instance="obs-vllm"}[1m])) / sum(rate(vllm:request_queue_time_seconds_count{instance="obs-vllm"}[1m]))',
        ),
        ("running requests", 'vllm:num_requests_running{instance="obs-vllm"}'),
        ("waiting requests", 'vllm:num_requests_waiting{instance="obs-vllm"}'),
        ("KV cache usage (0-1)", 'vllm:kv_cache_usage_perc{instance="obs-vllm"}'),
        ("preemptions (cumulative)", 'vllm:num_preemptions_total{instance="obs-vllm"}'),
        ("generation tokens/s", 'sum(rate(vllm:generation_tokens_total{instance="obs-vllm"}[1m]))'),
        ("prompt tokens/s", 'sum(rate(vllm:prompt_tokens_total{instance="obs-vllm"}[1m]))'),
        (
            "prefix cache hit ratio (1m)",
            'sum(rate(vllm:prefix_cache_hits_total{instance="obs-vllm"}[1m])) / sum(rate(vllm:prefix_cache_queries_total{instance="obs-vllm"}[1m]))',
        ),
        (
            "finished by reason /s",
            'sum by (finished_reason) (rate(vllm:request_success_total{instance="obs-vllm"}[1m]))',
        ),
    ],
    "app": [
        ("requests/s by status", 'sum by (status) (rate(llmapp_http_requests_total{route="/ask"}[1m]))'),
        ("error ratio 5m", "app:ask_error_ratio:rate5m"),
        ("error ratio 1h", "app:ask_error_ratio:rate1h"),
        ("burn rate 1h (x budget)", "app:ask_error_ratio:rate1h / 0.01"),
        (
            "latency p50 (s)",
            'histogram_quantile(0.5, sum by (le) (rate(llmapp_http_request_duration_seconds_bucket{route="/ask"}[1m])))',
        ),
        (
            "latency p95 (s)",
            'histogram_quantile(0.95, sum by (le) (rate(llmapp_http_request_duration_seconds_bucket{route="/ask"}[1m])))',
        ),
        (
            "stage mean latency (s)",
            "sum by (stage) (rate(llmapp_stage_duration_seconds_sum[1m])) / sum by (stage) (rate(llmapp_stage_duration_seconds_count[1m]))",
        ),
        (
            "client TTFT p95 by stage (s)",
            "histogram_quantile(0.95, sum by (le, stage) (rate(llmapp_llm_ttft_seconds_bucket[1m])))",
        ),
        ("outcomes /s", "sum by (outcome) (rate(llmapp_requests_total[1m]))"),
        ("LLM failures /s by type", 'sum by (outcome) (rate(llmapp_llm_requests_total{outcome!="ok"}[1m]))'),
        ("tokens/s by direction", "sum by (direction) (rate(llmapp_llm_tokens_total[1m]))"),
        (
            "est. cost USD per answered request",
            'sum(rate(llmapp_llm_cost_usd_total[2m])) / sum(rate(llmapp_requests_total{outcome="ok"}[2m]))',
        ),
    ],
    "infra": [
        ("GPU util %", "DCGM_FI_DEV_GPU_UTIL"),
        ("GPU power W", "DCGM_FI_DEV_POWER_USAGE"),
        ("memory available GiB", "node_memory_MemAvailable_bytes / 2^30"),
        ("GPU memory by workload GiB", 'gpu_process_memory_bytes{job="gpu-workloads"} / 2^30'),
    ],
}


def rng(expr, t0, t1, step):
    qs = urllib.parse.urlencode({"query": expr, "start": t0, "end": t1, "step": step})
    with urllib.request.urlopen(f"{PROM}/api/v1/query_range?{qs}", timeout=30) as r:
        return json.load(r)["data"]["result"]


def main():
    name, t0, t1 = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    which = sys.argv[4] if len(sys.argv) > 4 else "all"
    groups = SETS if which == "all" else {which: SETS[which]}
    step = max(5, int((t1 - t0) / 240))
    OUT.mkdir(parents=True, exist_ok=True)
    lines = [f"# scenario {name}  window {int(t0)}..{int(t1)} ({(t1 - t0) / 60:.1f} min), step {step}s", ""]
    for g, items in groups.items():
        lines.append(f"## {g}")
        for label, expr in items:
            lines.append(f"- {label}\n    PromQL: {expr}")
            for s in rng(expr, t0, t1, step):
                vals = [float(v) for _, v in s["values"] if v not in ("NaN", "+Inf", "-Inf")]
                if not vals:
                    continue
                tag = ",".join(
                    f"{k}={v}"
                    for k, v in s["metric"].items()
                    if k
                    not in (
                        "__name__",
                        "job",
                        "instance",
                        "engine",
                        "model_name",
                        "exported_engine",
                        "cluster",
                        "UUID",
                        "pci_bus_id",
                        "device",
                        "modelName",
                        "Hostname",
                        "gpu",
                        "DCGM_FI_DRIVER_VERSION",
                    )
                )
                pick = vals[:: max(1, len(vals) // 12)][:12]
                lines.append(
                    f"    {{{tag}}} min={min(vals):.4g} mean={sum(vals) / len(vals):.4g} max={max(vals):.4g} last={vals[-1]:.4g}\n      samples: "
                    + " ".join(f"{v:.3g}" for v in pick)
                )
        lines.append("")
    (OUT / f"{name}.txt").write_text("\n".join(lines))
    print("wrote", OUT / f"{name}.txt")


main()
