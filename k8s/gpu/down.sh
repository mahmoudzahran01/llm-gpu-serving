#!/usr/bin/env bash
# ./down.sh            stop vLLM + test pods (releases the GPU memory); plumbing stays so up.sh is fast
# ./down.sh plumbing   also remove the device plugin, the node label and the namespace
# ./down.sh all        also delete driver files / weights / images from the node (a clean slate; next up.sh ~10 min)
set -uo pipefail
source "$(dirname "$0")/scripts/_common.sh"
helm --kube-context "$CTX" -n "$NS" uninstall vllm 2>&1 | tail -1
$K -n "$NS" delete pod --all --wait=false 2>&1 | tail -3
case "${1:-}" in
  plumbing|all)
    helm --kube-context "$CTX" -n "$DP_NS" uninstall nvdp 2>&1 | tail -1
    $K delete ns "$NS" "$DP_NS" --wait=false
    $K label node "$GPU_NODE" nvidia.com/gpu.present-
    docker exec "$GPU_NODE" rm -f /var/run/cdi/k8s.device-plugin.nvidia.com-gpu.json ;;
esac
if [ "${1:-}" = all ]; then
  docker exec "$GPU_NODE" sh -c 'rm -rf /opt/nvidia-driver /models /var/cache/vllm /etc/cdi/nvidia-debug.yaml'
  docker exec "$GPU_NODE" crictl rmi "$VLLM_IMAGE" nvcr.io/nvidia/cuda:13.0.0-base-ubuntu24.04 2>&1 | tail -2
fi
echo "GPU memory now:"; nvidia-smi --query-compute-apps=pid,used_memory --format=csv
