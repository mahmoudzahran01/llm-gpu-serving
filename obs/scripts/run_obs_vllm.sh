#!/usr/bin/env bash
# A vLLM server instrumented for tracing: Qwen3-4B-Instruct (override MODEL), 10% of GPU memory.
#
# --network host: some hosts drop container -> host traffic on docker0 (ufw-style), so a bridge container cannot
# push OTLP spans to the collector. Host networking fixes that; the API binds 127.0.0.1:8020 so it is not
# exposed to the LAN.
# vLLM exports its own server-side spans (gen_ai.latency.time_in_queue, ..._to_first_token, e2e); they join the
# app's trace through the W3C `traceparent` header the app sends.
set -euo pipefail
MODEL="${MODEL:-Qwen/Qwen3-4B-Instruct-2507}"
IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm:26.10-py3}"
docker rm -f obs-vllm >/dev/null 2>&1 || true
docker run -d --name obs-vllm --gpus all --ipc=host --network host \
  -e OTEL_EXPORTER_OTLP_TRACES_INSECURE=true \
  -e OTEL_SERVICE_NAME=obs-vllm \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  "$IMAGE" \
  vllm serve "$MODEL" --host 127.0.0.1 --port 8020 \
    --gpu-memory-utilization 0.10 --max-model-len 6144 --enable-prefix-caching \
    --otlp-traces-endpoint localhost:4317
