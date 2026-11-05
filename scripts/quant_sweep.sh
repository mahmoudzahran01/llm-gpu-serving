#!/usr/bin/env bash
# Serve each model variant with vLLM, then measure accuracy (optional, ACCURACY_CMD) and speed (bench).
# Same server flags for every variant (see lib_serve.sh).
#
# Usage: scripts/quant_sweep.sh <tag-prefix> <model-path-or-hf-id> [<model> ...]
#   scripts/quant_sweep.sh final runs/merged/final runs/quant/final-fp8 runs/quant/final-w4a16
set -uo pipefail
cd "$(dirname "$0")/.."
source scripts/lib_serve.sh
PREFIX=$1; shift
for MODEL in "$@"; do
  NAME=$(basename "$MODEL")
  serve_vllm "$MODEL" || { echo "$NAME: could not serve"; continue; }
  serve_log "${PREFIX}-${NAME}"
  accuracy "${PREFIX}-${NAME}"
  bench "${PREFIX}-${NAME}"
  stop_vllm
  echo "$NAME done"
done
stop_vllm
