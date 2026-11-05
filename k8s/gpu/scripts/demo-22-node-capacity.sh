#!/usr/bin/env bash
# What the cluster knows about the GPU after the device plugin registered.
source "$(dirname "$0")/_common.sh"
echo "### host view"; run nvidia-smi -L
run $K get nodes -o custom-columns='NODE:.metadata.name,GPU-CAPACITY:.status.capacity.nvidia\.com/gpu,GPU-ALLOCATABLE:.status.allocatable.nvidia\.com/gpu,GPU-LABEL:.metadata.labels.nvidia\.com/gpu\.present'
echo; echo "\$ kubectl describe node $GPU_NODE   (Capacity / Allocatable / Allocated resources)"
$K describe node $GPU_NODE | sed -n '/^Capacity:/,/^System Info:/p' | grep -v "^System Info"
$K describe node $GPU_NODE | sed -n '/^Allocated resources:/,/^Events:/p' | grep -v "^Events"
echo; echo "### what the plugin wrote for containerd (CDI spec, abridged) - this is how a pod gets /dev/nvidia0 and libcuda"
run docker exec $GPU_NODE ls -l /var/run/cdi
docker exec $GPU_NODE sh -c "tr -d '\n ' < /var/run/cdi/k8s.device-plugin.nvidia.com-gpu.json | cut -c1-700"; echo " ..."
echo; echo "### the same node's kubelet device-manager view"
run $K -n $DP_NS get ds,pods -o wide
echo; echo "### GB10 / unified-memory notes"
echo "\$ nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv"; nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv
echo "(memory columns are [N/A]: NVML cannot split unified memory; the device plugin does not need it - it only counts devices.)"
