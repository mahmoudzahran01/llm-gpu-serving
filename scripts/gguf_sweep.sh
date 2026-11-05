#!/usr/bin/env bash
# llama.cpp path: HF checkpoint -> GGUF (bf16) -> Q4_K_M, served by llama-server, same bench as the vLLM variants.
# Usage: LLAMA_CPP=/path/to/llama.cpp scripts/gguf_sweep.sh <merged-model-dir> [tag]
# Needs a CUDA-enabled llama.cpp build (build/bin/llama-quantize, build/bin/llama-server).
set -uo pipefail
cd "$(dirname "$0")/.."
source scripts/lib_serve.sh
: "${LLAMA_CPP:?set LLAMA_CPP to your llama.cpp checkout}"
MODEL=$1; TAG=${2:-gguf-q4km}
mkdir -p runs/gguf
python "$LLAMA_CPP/convert_hf_to_gguf.py" "$MODEL" --outtype bf16 --outfile runs/gguf/model-bf16.gguf > runs/gguf/convert.log 2>&1 || { echo "convert failed"; exit 1; }
"$LLAMA_CPP/build/bin/llama-quantize" runs/gguf/model-bf16.gguf runs/gguf/model-q4km.gguf Q4_K_M > runs/gguf/quantize.log 2>&1 || exit 1
"$LLAMA_CPP/build/bin/llama-server" -m runs/gguf/model-q4km.gguf --alias m --host 127.0.0.1 --port "$PORT" \
  -ngl 99 -c 98304 --parallel 16 > runs/gguf/server.log 2>&1 &
pid=$!
until curl -sf "localhost:$PORT/v1/models" >/dev/null; do kill -0 $pid 2>/dev/null || exit 1; sleep 5; done
accuracy "$TAG"
bench "$TAG"
kill $pid; wait $pid 2>/dev/null
ls -l runs/gguf/*.gguf > runs/gguf/sizes.txt
