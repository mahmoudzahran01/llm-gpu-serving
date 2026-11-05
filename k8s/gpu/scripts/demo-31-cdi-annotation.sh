#!/usr/bin/env bash
# containerd does not act on cdi.k8s.io pod annotations; the device plugin has to use the CRI CDIDevices field.
source "$(dirname "$0")/_common.sh"
echo "### 1. a raw CDI spec for the toolkit's device nvidia.com/gpu=all, valid inside the node (host paths -> /opt/nvidia-driver copy)"
python3 scripts/make-node-cdi-spec.py "$CDI_SPEC" /opt/nvidia-driver | docker exec -i $GPU_NODE sh -c 'mkdir -p /etc/cdi && cat > /etc/cdi/nvidia-debug.yaml'
run docker exec $GPU_NODE sh -c 'ls -l /etc/cdi; grep -E "^kind:|- name:" /etc/cdi/nvidia-debug.yaml'
echo; echo "### 2. pod WITHOUT a GPU limit, annotated cdi.k8s.io/probe: nvidia.com/gpu=all"
$K -n $NS delete pod cdi-annotation-probe --ignore-not-found --wait=true >/dev/null
run $K apply -f manifests/cdi-annotation-probe.yaml
$K -n $NS wait --for=jsonpath='{.status.phase}'=Succeeded pod/cdi-annotation-probe --timeout=90s >/dev/null
run $K -n $NS logs cdi-annotation-probe
echo; echo "### result: nothing injected, no error. containerd ignores the annotation, so the device plugin must return the CDI name via the CRI field (deviceListStrategy: cdi-cri)"
$K -n $NS delete pod cdi-annotation-probe --wait=false >/dev/null
docker exec $GPU_NODE rm -f /etc/cdi/nvidia-debug.yaml && echo "debug spec removed from the node"
