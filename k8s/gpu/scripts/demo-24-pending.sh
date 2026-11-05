#!/usr/bin/env bash
# The key scheduling lesson: nvidia.com/gpu is an integer resource. Ask for more than exists -> Pending.
source "$(dirname "$0")/_common.sh"
$K -n $NS delete pod wants-two-gpus --ignore-not-found --wait=true >/dev/null
run $K apply -f manifests/gpu-x2-pending.yaml
sleep 8
run $K -n $NS get pod wants-two-gpus -o wide
echo; echo "\$ kubectl describe pod wants-two-gpus   (Events)"
$K -n $NS describe pod wants-two-gpus | sed -n '/^Events:/,$p'
echo; run $K -n $NS get events --field-selector involvedObject.name=wants-two-gpus -o custom-columns='REASON:.reason,MESSAGE:.message'
echo; echo "### reading the message: 0/3 nodes: 1 untolerated taint (control-plane) + 2 Insufficient nvidia.com/gpu"
echo "###   (worker has 0 GPUs, worker2 has 1 < 2). Nothing is 'queued' on a GPU; the pod just never leaves Pending."
run $K get nodes -o custom-columns='NODE:.metadata.name,GPU-ALLOCATABLE:.status.allocatable.nvidia\.com/gpu'
$K -n $NS delete pod wants-two-gpus --wait=false >/dev/null
