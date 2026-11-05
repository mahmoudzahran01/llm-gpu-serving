#!/usr/bin/env bash
# Bring up the whole observability stack. Idempotent. Run from anywhere.
#   obs/scripts/up.sh          # stack + gpu exporter + alert sink + demo service
#   obs/scripts/up.sh stack    # only the docker compose stack
# Needs obs/.env with GRAFANA_ADMIN_PASSWORD (see obs/.env.example) and a virtualenv with `pip install -e ".[obs]"`
# (PY=... to point elsewhere).
set -euo pipefail
OBS="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$OBS/.." && pwd)"
RUN="$ROOT/runs/obs"; mkdir -p "$RUN/logs" "$RUN/pids" "$OBS/data"/{prometheus,alertmanager,grafana,tempo,loki}
export OBS_UID="$(id -u)" OBS_GID="$(id -g)"
PY="${PY:-$ROOT/.venv/bin/python}"
[ -f "$OBS/.env" ] || { echo "missing $OBS/.env - copy .env.example and set GRAFANA_ADMIN_PASSWORD" >&2; exit 1; }

docker compose --env-file "$OBS/.env" -f "$OBS/docker-compose.yml" up -d
[ "${1:-}" = "stack" ] && exit 0

start() {  # start <name> <cmd...>  - background process with a pidfile (no pkill, ever)
  local name="$1"; shift
  if [ -f "$RUN/pids/$name.pid" ] && kill -0 "$(cat "$RUN/pids/$name.pid")" 2>/dev/null; then echo "$name already running"; return; fi
  nohup "$@" >> "$RUN/$name.out" 2>&1 &
  echo $! > "$RUN/pids/$name.pid"; echo "started $name (pid $!)"
}
start gpu-exporter "$PY" "$OBS/exporters/gpu_exporter.py"
start alert-sink   "$PY" "$OBS/scripts/alert_sink.py" "$RUN/alerts.jsonl"
cd "$ROOT"
FAULTS_ENABLED=1 APP_LOG_FILE="$RUN/logs/app.jsonl" \
  start demo-app "$PY" -m uvicorn llmserve.obs.demo_app:create_app --factory --host 127.0.0.1 --port 8030 --no-access-log
echo "Grafana http://127.0.0.1:3030  Prometheus :9090  Alertmanager :9093  Tempo :3200  Loki :3100  app :8030"
