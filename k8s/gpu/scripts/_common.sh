# sourced by the gpu-lab scripts
export PATH=$HOME/.local/bin:$PATH
GPU_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$GPU_DIR"
CTX=${CTX:-kind-lab}            # ALWAYS pass --context: never switch the user's current-context
K="kubectl --context $CTX"
CLUSTER=${CLUSTER:-lab}
GPU_NODE=${GPU_NODE:-lab-worker2}   # the kind node container that gets the GPU
NS=gpu-lab                                 # our own namespace (vLLM, test pods, prometheus)
DP_NS=nvidia-device-plugin
DP_VERSION=${DP_VERSION:-0.20.1}
VLLM_IMAGE=${VLLM_IMAGE:-nvcr.io/nvidia/vllm:26.10-py3}
MODEL_NAME=Qwen3-4B-Instruct-2507
HF_SNAPSHOT_GLOB=$HOME/.cache/huggingface/hub/models--Qwen--Qwen3-4B-Instruct-2507/snapshots/*
CDI_SPEC=${CDI_SPEC:-/var/run/cdi/nvidia.yaml}
run() { echo; echo "\$ $*"; "$@"; }
# GPU memory guard: this box is shared. Wait (long poll) until enough memory is available.
need_mem_gb=${NEED_MEM_GB:-25}
wait_for_memory() {
  while :; do
    avail=$(free -g | awk '/^Mem:/ {print $7}')
    [ "$avail" -ge "$need_mem_gb" ] && { echo "memory ok: ${avail} GB available (need ${need_mem_gb})"; return 0; }
    echo "$(date +%T) only ${avail} GB available (need ${need_mem_gb}) - waiting 5 min"; sleep 300
  done
}
