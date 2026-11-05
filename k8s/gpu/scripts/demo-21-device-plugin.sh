#!/usr/bin/env bash
# The device plugin on a box with no sudo: two real failures and the fix. (Re-runnable; leaves the plugin healthy.)
source "$(dirname "$0")/_common.sh"
SOCK=/var/lib/kubelet/device-plugins/kubelet.sock
plugin_logs() { $K -n $DP_NS logs -l app.kubernetes.io/name=nvidia-device-plugin --tail="${1:-4}" "${@:2}" 2>&1 | cut -c1-200; }
wait_ready() { $K -n $DP_NS rollout status ds/nvdp-nvidia-device-plugin --timeout=90s 2>&1 | tail -1; }
gpu_alloc() { $K get node $GPU_NODE -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'; echo; }

echo "### A. chart default: plugin runs as root. uid 0's inotify quota is used up (evidence/20) -> crash loop"
helm --kube-context $CTX upgrade nvdp nvdp/nvidia-device-plugin --version $DP_VERSION -n $DP_NS -f device-plugin/values.yaml \
  --set securityContext.runAsUser=0 --set securityContext.runAsGroup=0 >/dev/null
$K -n $DP_NS delete pod -l app.kubernetes.io/name=nvidia-device-plugin --wait=true >/dev/null; sleep 30
run $K -n $DP_NS get pods
echo; echo "\$ kubectl logs <plugin> | tail -3"
P=$($K -n $DP_NS get pods -l app.kubernetes.io/name=nvidia-device-plugin -o name | head -1)
for _ in 1 2 3 4 5 6 7 8; do $K -n $DP_NS logs "$P" --tail=3 2>&1 | grep -q inotify && break; sleep 8; done
$K -n $DP_NS logs "$P" --tail=3 2>&1 | cut -c1-200
echo; echo "### B. run as uid 65534 (own inotify bucket) - but kubelet.sock is srwxr-xr-x root: a non-root client cannot connect()"
helm --kube-context $CTX upgrade nvdp nvdp/nvidia-device-plugin --version $DP_VERSION -n $DP_NS -f device-plugin/values.yaml >/dev/null
docker exec $GPU_NODE chmod 0755 $SOCK
$K -n $DP_NS delete pod -l app.kubernetes.io/name=nvidia-device-plugin --wait=true >/dev/null; sleep 25
echo "\$ ls -l $SOCK (in the node)"; docker exec $GPU_NODE ls -l $SOCK
echo "\$ kubectl logs <plugin> | grep -E 'Registered|register|Failed|Starting to serve'"; plugin_logs 40 | grep -E 'Registered|register|Failed|Starting to serve'

echo; echo "### C. fix: chmod 0666 kubelet.sock (lab-only; durable fix = raise fs.inotify.max_user_instances and run as root)"
docker exec $GPU_NODE chmod 0666 $SOCK
$K -n $DP_NS delete pod -l app.kubernetes.io/name=nvidia-device-plugin --wait=true >/dev/null; wait_ready; sleep 10
echo "\$ kubectl logs <plugin> | grep -E 'Registered|Starting to serve|Ignoring'"; plugin_logs 80 | grep -E 'Registered|Starting to serve|Ignoring'
echo -n "allocatable nvidia.com/gpu on $GPU_NODE: "; gpu_alloc
