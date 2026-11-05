#!/usr/bin/env bash
# Failure / saturation drills against the running stack (see obs/README.md, "Drills").
#   B  vLLM saturation (long unique prompts straight at obs-vllm)  -> KV cache, queue, TTFT, (preemption) alerts
#   S  stalled LLM backend on every /ask (fast failures)            -> 5xx burst -> SLO burn-rate alert
set -euo pipefail
OBS="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(cd "$OBS/.." && pwd)"; RUN="$ROOT/runs/obs"
cd "$ROOT"; PY="${PY:-$ROOT/.venv/bin/python}"
mkdir -p "$RUN"
mark() { echo "$1 $2 $3 $(date -Is)" | tee -a "$RUN/scenarios.log"; }
which="${1:-all}"
if [[ "$which" == all || "$which" == B ]]; then
  t0=$(date +%s)
  "$PY" -m llmserve.obs.loadgen raw --concurrency 24 --duration 150 --prompt-words 550 --max-tokens 300 \
        --annotate "B: saturate obs-vllm (24 concurrent long prompts)" 2>&1 | tee "$RUN/load-B.out"
  mark B_window "$t0" "$(date +%s)"; sleep 45
fi
if [[ "$which" == all || "$which" == S ]]; then
  t0=$(date +%s)
  "$PY" -m llmserve.obs.loadgen --phases "8:180" --fault llm_timeout --fault-rate 1.0 \
        --out "$RUN/load-S-llm-timeout.jsonl" --annotate "S: LLM timeout on 100% of requests" 2>&1 | tee "$RUN/load-S.out"
  mark S_window "$t0" "$(date +%s)"
fi
