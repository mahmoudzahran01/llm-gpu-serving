#!/usr/bin/env bash
# Make ONE existing kind node GPU-capable (rootless, no restart, nothing on the other nodes is touched):
#   1. copy the NVIDIA user-space driver files + CDI hook into the node under /opt/nvidia-driver
#   2. copy the model weights (dereferenced HF snapshot) into the node under /models
#   3. label the node nvidia.com/gpu.present=true (what the device-plugin DaemonSet selects on)
# Why copy instead of bind-mount: the node container already exists and docker cannot add a bind mount to a
# running container. (A fresh cluster would use kind extraMounts - see kind/gen-kind-config.sh - but a fresh
# cluster cannot start on this box, see evidence/20-*.)
# Idempotent. Usage: scripts/node-inject.sh [debug-spec]
source "$(dirname "$0")/_common.sh"
DEST=/opt/nvidia-driver
echo "== 1. driver files -> $GPU_NODE:$DEST"
LIST=$(mktemp)
{
  awk '/hostPath:/ {print $NF}' "$CDI_SPEC" | while read -r f; do [ -f "$f" ] && echo "$f"; done
  ls /usr/lib/aarch64-linux-gnu/libcuda.so* /usr/lib/aarch64-linux-gnu/libnvidia-*.so* /usr/lib/aarch64-linux-gnu/libnvcuvid.so* 2>/dev/null
  echo /usr/bin/nvidia-cdi-hook; echo /usr/bin/nvidia-ctk
} | sort -u | sed 's#^/##' > "$LIST"
docker exec "$GPU_NODE" mkdir -p $DEST
tar -C / -cf - -T "$LIST" 2>/dev/null | docker exec -i "$GPU_NODE" tar -C $DEST -xf -
rm -f "$LIST"
docker exec "$GPU_NODE" sh -c "ls $DEST/usr/lib/aarch64-linux-gnu | wc -l | sed 's/^/driver files copied: /'; du -sh $DEST | cut -f1 | sed 's/^/size: /'"

echo "== 2. model weights -> $GPU_NODE:/models/$MODEL_NAME"
if docker exec "$GPU_NODE" test -f /models/$MODEL_NAME/model-00003-of-00003.safetensors; then
  echo "already there"
else
  docker exec "$GPU_NODE" mkdir -p /models/$MODEL_NAME
  SNAP=$(ls -d $HF_SNAPSHOT_GLOB | head -1)
  tar -C "$SNAP" -chf - . | docker exec -i "$GPU_NODE" tar -C /models/$MODEL_NAME -xf -
fi
docker exec "$GPU_NODE" du -sh /models/$MODEL_NAME

echo "== 3. label the node"
$K label node "$GPU_NODE" nvidia.com/gpu.present=true --overwrite

echo "== 4. let the NON-ROOT device plugin write its socket and CDI spec on the node"
# kubelet creates /var/lib/kubelet/device-plugins 0755 root; the plugin (uid 65534, see device-plugin/values.yaml)
# must create nvidia-gpu.sock there and the CDI spec in /var/run/cdi. A lab-only shortcut: in production you
# would raise fs.inotify.max_user_instances (needs root) and run the plugin as root as NVIDIA intends.
# Connecting to a unix socket needs WRITE permission on it, so kubelet.sock (srwxr-xr-x root) must be opened too.
# kubelet recreates that socket on restart, so re-run this script (or up.sh) after restarting the node.
docker exec "$GPU_NODE" sh -c 'mkdir -p /var/run/cdi /run/nvidia/mps/shm && chmod 0777 /var/lib/kubelet/device-plugins /var/run/cdi /run/nvidia/mps /run/nvidia/mps/shm && chmod 0666 /var/lib/kubelet/device-plugins/kubelet.sock'

if [ "${1:-}" = debug-spec ]; then
  echo "== 5. (debug only) raw CDI device nvidia.com/gpu=all in the node, to test CDI before the device plugin"
  python3 scripts/make-node-cdi-spec.py "$CDI_SPEC" $DEST | docker exec -i "$GPU_NODE" sh -c 'mkdir -p /etc/cdi && cat > /etc/cdi/nvidia-debug.yaml'
fi
