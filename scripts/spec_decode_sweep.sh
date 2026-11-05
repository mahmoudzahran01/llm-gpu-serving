#!/usr/bin/env bash
# Speculative decoding on one model: none vs n-gram (prompt lookup) vs a small draft model.
# Each variant is benchmarked at concurrency 1/8/16 and its acceptance counters are saved
# (runs/bench/spec-<variant>.metrics.txt: spec_decode_num_accepted_tokens_total / num_draft_tokens_total).
#
# Usage: scripts/spec_decode_sweep.sh <model-dir> [draft-model]      (draft defaults to Qwen/Qwen3-0.6B)
set -uo pipefail
cd "$(dirname "$0")/.."
source scripts/lib_serve.sh
MODEL=$1; DRAFT=${2:-Qwen/Qwen3-0.6B}
declare -A CFG=(
  [none]=""
  [ngram]='{"method":"ngram","num_speculative_tokens":4,"prompt_lookup_max":4,"prompt_lookup_min":2}'
  [draft]="{\"model\":\"$DRAFT\",\"num_speculative_tokens\":4}")
for v in none ngram draft; do
  echo "spec decoding: $v"
  if [[ -n ${CFG[$v]} ]]; then serve_vllm "$MODEL" --speculative-config "${CFG[$v]}" || continue
  else serve_vllm "$MODEL" || continue; fi
  accuracy "spec-$v"
  bench "spec-$v"
  curl -s "localhost:$PORT/metrics" | grep -E "^vllm:spec_decode" > "runs/bench/spec-$v.metrics.txt" || true
  stop_vllm
done
