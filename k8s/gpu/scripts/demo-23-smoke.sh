#!/usr/bin/env bash
# A pod that requests the GPU sees it; the same pod without a request on the same node sees nothing.
source "$(dirname "$0")/_common.sh"
$K -n $NS delete pod smoke-gpu smoke-no-gpu --ignore-not-found --wait=true >/dev/null
run $K apply -f manifests/smoke-gpu.yaml -f manifests/smoke-no-gpu.yaml
for p in smoke-gpu smoke-no-gpu; do $K -n $NS wait --for=jsonpath='{.status.phase}'=Succeeded pod/$p --timeout=120s >/dev/null; done
run $K -n $NS get pods -o wide
echo; echo "### smoke-gpu (limits: nvidia.com/gpu: 1)";   $K -n $NS logs smoke-gpu
echo; echo "### smoke-no-gpu (no request, same node)";     $K -n $NS logs smoke-no-gpu
echo; echo "### what the scheduler/kubelet recorded for the GPU pod"
$K -n $NS get pod smoke-gpu -o jsonpath='{range .spec.containers[*]}{.name}{" limits="}{.resources.limits}{"\n"}{end}'
$K -n $NS get pod smoke-gpu -o jsonpath='{"node="}{.spec.nodeName}{"\n"}'
