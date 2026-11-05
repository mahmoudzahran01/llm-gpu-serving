#!/usr/bin/env bash
# Time-slicing: advertise 1 GPU as 2. Needs the vLLM release to be down (the plugin re-registers with new device IDs).
source "$(dirname "$0")/_common.sh"
gpu_alloc() { $K get node $GPU_NODE -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'; }
mk() { for n in "$@"; do sed "s/__NAME__/$n/" manifests/timeslice-pod.yaml.tpl | $K apply -f - >/dev/null; done; }
waitdone() { for p in "$@"; do $K -n $NS wait --for=jsonpath='{.status.phase}'=Succeeded pod/$p --timeout=180s >/dev/null; done; }
echo "### 0. stop vLLM so the GPU is free and idle for this experiment"
helm --kube-context $CTX -n $NS uninstall vllm 2>&1 | tail -1
$K -n $NS delete pod -l app=timeslice --ignore-not-found --wait=true >/dev/null
echo "allocatable nvidia.com/gpu = $(gpu_alloc)"

echo; echo "### 1. baseline, no time-slicing: ONE burner pod alone"
mk ts-a
waitdone ts-a; $K -n $NS logs ts-a | tail -2; $K -n $NS delete pod ts-a --wait=true >/dev/null

echo; echo "### 2. same cluster, three pods at once WITHOUT time-slicing (capacity 1): a runs, b and c wait for the GPU"
mk ts-a ts-b ts-c; sleep 12
run $K -n $NS get pods -l app=timeslice -o wide
$K -n $NS describe pod ts-c | sed -n '/^Events:/,$p' | tail -3
$K -n $NS delete pod -l app=timeslice --wait=true >/dev/null

echo; echo "### 3. enable time-slicing: replicas=2"
run cat device-plugin/values-timeslicing.yaml
run helm --kube-context $CTX upgrade nvdp nvdp/nvidia-device-plugin --version $DP_VERSION -n $DP_NS -f device-plugin/values.yaml -f device-plugin/values-timeslicing.yaml
$K -n $DP_NS rollout status ds/nvdp-nvidia-device-plugin --timeout=120s | tail -1
for _ in $(seq 1 30); do [ "$(gpu_alloc)" = 2 ] && break; sleep 3; done
echo "allocatable nvidia.com/gpu = $(gpu_alloc)   (one physical GPU)"
run nvidia-smi -L
$K -n $DP_NS logs -l app.kubernetes.io/name=nvidia-device-plugin -c nvidia-device-plugin-ctr --tail=200 | grep -iE "replicas|timeSlicing|Registered|sharing" | cut -c1-200 | head -6

echo; echo "### 4. a, b, c at once: a and b share the physical GPU, c is Pending (2 slots)"
mk ts-a ts-b ts-c; sleep 25
run $K -n $NS get pods -l app=timeslice -o wide
echo; echo "\$ nvidia-smi --query-compute-apps=pid,used_memory  (host: two CUDA processes from two pods on ONE GPU, plus other tenants)"
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
waitdone ts-a ts-b
echo; echo "### 5. what each pod saw and got"
for p in ts-a ts-b; do $K -n $NS logs $p | tail -2; done
$K -n $NS describe pod ts-c | sed -n '/^Events:/,$p' | tail -2
$K -n $NS delete pod -l app=timeslice --wait=true >/dev/null

echo; echo "### 6. revert to one-GPU-one-pod"
helm --kube-context $CTX upgrade nvdp nvdp/nvidia-device-plugin --version $DP_VERSION -n $DP_NS -f device-plugin/values.yaml >/dev/null
$K -n $DP_NS rollout status ds/nvdp-nvidia-device-plugin --timeout=120s | tail -1
for _ in $(seq 1 30); do [ "$(gpu_alloc)" = 1 ] && break; sleep 3; done
echo "allocatable nvidia.com/gpu = $(gpu_alloc)"
