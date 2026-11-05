#!/usr/bin/env bash
# capture.sh <name> <start_epoch> <end_epoch>  -> evidence for a scenario window:
#   obs/evidence/metrics/<name>.txt           PromQL results (what the dashboards plot)
#   obs/evidence/screens/<name>-{llm,app,infra}.png   Grafana screenshots of the same window
# Needs a virtualenv with playwright (`pip install playwright && python -m playwright install chromium`)
# and GRAFANA_ADMIN_PASSWORD in the environment (Explore needs a non-viewer role).
set -euo pipefail
OBS="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(cd "$OBS/.." && pwd)"
NAME="$1"; T0="$2"; T1="$3"
PAD=30
PY="${PY:-$ROOT/.venv/bin/python}"
mkdir -p "$OBS/evidence/"{metrics,screens}
"$PY" "$OBS/scripts/evidence_dump.py" "$NAME" "$T0" "$T1" all
"$PY" "$OBS/scripts/shoot.py" dash llm-serving "$OBS/evidence/screens/$NAME-llm.png" --from $((T0-PAD)) --to $((T1+PAD)) --var instance=obs-vllm --height 2700
"$PY" "$OBS/scripts/shoot.py" dash app         "$OBS/evidence/screens/$NAME-app.png" --from $((T0-PAD)) --to $((T1+PAD)) --height 2600
"$PY" "$OBS/scripts/shoot.py" dash infra       "$OBS/evidence/screens/$NAME-infra.png" --from $((T0-PAD)) --to $((T1+PAD)) --height 2300
