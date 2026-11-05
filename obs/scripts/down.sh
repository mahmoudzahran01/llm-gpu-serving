#!/usr/bin/env bash
# Stop everything this stack started (data in obs/data is kept). `down.sh all` also stops obs-vllm.
OBS="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(cd "$OBS/.." && pwd)"; RUN="$ROOT/runs/obs"
for f in "$RUN"/pids/*.pid; do [ -f "$f" ] || continue; kill "$(cat "$f")" 2>/dev/null && echo "stopped $(basename "$f" .pid)"; rm -f "$f"; done
export OBS_UID="$(id -u)" OBS_GID="$(id -g)" GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-unused}"
docker compose -f "$OBS/docker-compose.yml" down
[ "${1:-}" = "all" ] && docker rm -f obs-vllm 2>/dev/null && echo "stopped obs-vllm"
exit 0
