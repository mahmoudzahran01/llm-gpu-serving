#!/usr/bin/env bash
# End-of-session teardown: no vLLM pod, no GPU memory held by this lab. Plumbing (device plugin, node files) stays.
source "$(dirname "$0")/_common.sh"
echo "### ./down.sh"
../gpu/down.sh 2>&1
sleep 20
echo; echo "### verify"
run $K -n $NS get pods,svc,deploy
run helm --kube-context $CTX list -A
run $K get node $GPU_NODE -o custom-columns='NODE:.metadata.name,GPU-ALLOCATABLE:.status.allocatable.nvidia\.com/gpu'
$K describe node $GPU_NODE | sed -n '/^Allocated resources:/,/^Events:/p' | grep -E "Resource|nvidia"
run nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
run free -g
echo; echo "### the other workload on this cluster (namespace rag, not part of this repository) is untouched"
run $K -n rag get pods
