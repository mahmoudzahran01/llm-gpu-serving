# Shared helpers: start/stop a vLLM server on 127.0.0.1:$PORT with a FIXED memory budget, then benchmark it.
# Usage: source scripts/lib_serve.sh; serve_vllm <model-or-dir> [extra vllm flags...]; bench <tag>; stop_vllm
#
# Every variant of a comparison must get the same server flags, or the comparison measures the flags. The fixed
# KV cache (--kv-cache-memory) and max length below are what the published sweeps used (5 GiB, 6144 tokens).
#
# Environment: VLLM_IMAGE, PORT, KV_BYTES, MAX_LEN, GPU_UTIL,
#   BENCH_ARGS     extra arguments for llmserve.bench, e.g. "--system-prompt-file prompt.txt --questions-file q.jsonl"
#   ACCURACY_CMD   optional command run after serving as `ACCURACY_CMD <tag>` with BASE_URL exported
#                  (the published accuracy numbers came from the text-to-SQL evaluator of the companion repository)
VLLM_IMAGE=${VLLM_IMAGE:-nvcr.io/nvidia/vllm:26.10-py3}
PORT=${PORT:-8010}
KV_BYTES=${KV_BYTES:-5368709120}
MAX_LEN=${MAX_LEN:-6144}
GPU_UTIL=${GPU_UTIL:-0.2}     # only feeds vLLM's start-up "enough free memory?" check when --kv-cache-memory is set
PY=${PY:-python}

serve_vllm() {
  local model=$1; shift
  [[ -d $model ]] && model=/lab/$model
  docker rm -f lab-vllm >/dev/null 2>&1 || true
  docker run -d --name lab-vllm --gpus all --ipc=host -p "127.0.0.1:$PORT:8000" \
    -v "$HOME/.cache/huggingface:/root/.cache/huggingface" -v "$PWD:/lab" \
    "$VLLM_IMAGE" vllm serve "$model" --served-model-name m \
    --kv-cache-memory "$KV_BYTES" --gpu-memory-utilization "$GPU_UTIL" --max-model-len "$MAX_LEN" \
    --enable-prefix-caching "$@" >/dev/null
  until curl -sf "localhost:$PORT/v1/models" >/dev/null; do
    docker ps -q -f name=lab-vllm | grep -q . || { echo "vLLM exited:"; docker logs --tail 30 lab-vllm 2>&1 | grep -E "Error|error" | tail -5; return 1; }
    sleep 5
  done
}

stop_vllm() { docker rm -f lab-vllm >/dev/null 2>&1 || true; }

bench() {  # tag
  # shellcheck disable=SC2086
  $PY -m llmserve.bench --base-url "http://localhost:$PORT/v1" --model m --levels 1,8,16 --requests 32 --tag "$1" \
    --out-dir runs/bench ${BENCH_ARGS:-} > /dev/null && echo "  bench $1 done"
}

accuracy() {  # tag
  [[ -n ${ACCURACY_CMD:-} ]] || return 0
  BASE_URL="http://localhost:$PORT/v1" $ACCURACY_CMD "$1"
}

serve_log() {  # tag  -> weight memory and KV-cache size lines from the server log
  mkdir -p runs/bench
  docker logs lab-vllm 2>&1 | grep -E "Model loading took|Available KV cache|GPU KV cache size" > "runs/bench/$1.serve.txt" || true
}
