#!/usr/bin/env bash
# Start / stop / restart just the demo service (uses the pidfile; never pkill).
#   app.sh restart                                     # normal
#   LLM_BASE_URL=http://localhost:8010 LLM_MODEL=m app.sh restart    # point at another backend
set -euo pipefail
OBS="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(cd "$OBS/.." && pwd)"; RUN="$ROOT/runs/obs"
PY="${PY:-$ROOT/.venv/bin/python}"
mkdir -p "$RUN/logs" "$RUN/pids"
stop() { [ -f "$RUN/pids/demo-app.pid" ] && kill "$(cat "$RUN/pids/demo-app.pid")" 2>/dev/null && sleep 1; rm -f "$RUN/pids/demo-app.pid"; return 0; }
start() {
  cd "$ROOT"
  FAULTS_ENABLED=1 APP_LOG_FILE="$RUN/logs/app.jsonl" \
    nohup "$PY" -m uvicorn llmserve.obs.demo_app:create_app --factory --host 127.0.0.1 --port 8030 --no-access-log >> "$RUN/demo-app.out" 2>&1 &
  echo $! > "$RUN/pids/demo-app.pid"; echo "demo-app pid $!"
}
case "${1:-restart}" in stop) stop;; start) start;; restart) stop; start;; esac
