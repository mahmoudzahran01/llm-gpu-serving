#!/usr/bin/env bash
# Bring up "vLLM on Kubernetes with a real GPU" (idempotent, no sudo).
#   ./up.sh                 # GPU plumbing + vLLM
#   SKIP_VLLM=1 ./up.sh     # GPU plumbing only (device plugin, node files); no GPU memory used
#
# What it does, in order:
#   1. preflight: NVML works on the host, the kind node exists, enough free memory (shared GPU box!)
#   2. kind load the images into ONE node (the vLLM image is 27 GB: the first run takes ~9 min)
#   3. scripts/node-inject.sh   driver files + weights + node label (+ permissions for the non-root plugin)
#   4. helm: NVIDIA device plugin (CDI mode, no nvidia runtime)  -> node advertises nvidia.com/gpu
#   5. helm: charts/vllm-gpu -> Deployment requesting nvidia.com/gpu: 1, Service
# Why the existing lab cluster and not a new one: evidence/20 (uid-0 inotify quota is exhausted).
set -euo pipefail
source "$(dirname "$0")/scripts/_common.sh"

echo "== 1. preflight"
nvidia-smi -L
$K get node "$GPU_NODE" >/dev/null
docker ps --format '{{.Names}}' | grep -qx "$GPU_NODE"

echo "== 2. images into node $GPU_NODE"
IMAGES=(nvcr.io/nvidia/cuda:13.0.0-base-ubuntu24.04)
[ -n "${SKIP_VLLM:-}" ] || IMAGES+=("$VLLM_IMAGE")
for img in "${IMAGES[@]}"; do
  docker exec "$GPU_NODE" crictl images 2>/dev/null | awk '{print $1":"$2}' | grep -qx "$img" \
    || kind load docker-image "$img" --name "$CLUSTER" --nodes "$GPU_NODE"
done

echo "== 3. node: driver files, weights, label, permissions"
scripts/node-inject.sh

echo "== 4. NVIDIA device plugin $DP_VERSION"
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin >/dev/null 2>&1 || true
helm repo update nvdp >/dev/null
helm --kube-context "$CTX" upgrade --install nvdp nvdp/nvidia-device-plugin --version "$DP_VERSION" \
  -n "$DP_NS" --create-namespace -f device-plugin/values.yaml --wait --timeout 3m
for _ in $(seq 1 30); do
  [ "$($K get node "$GPU_NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}')" -ge 1 ] 2>/dev/null && break; sleep 2
done
$K describe node "$GPU_NODE" | grep -E "nvidia.com/gpu"

$K get ns "$NS" >/dev/null 2>&1 || $K create ns "$NS"
[ -n "${SKIP_VLLM:-}" ] && { echo "SKIP_VLLM set - done."; exit 0; }

echo "== 5. vLLM (needs ~15 GB of the shared unified memory)"
wait_for_memory
helm --kube-context "$CTX" upgrade --install vllm charts/vllm-gpu -n "$NS" --wait --timeout 30m
$K -n "$NS" get pods,svc -o wide
echo "Try:  kubectl --context $CTX -n $NS port-forward svc/vllm 18200:8000   then   curl localhost:18200/v1/models"
