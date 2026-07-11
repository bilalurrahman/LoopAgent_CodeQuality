#!/usr/bin/env bash
# run-local.sh — start the quality loop + dashboard.
#
#   ./run-local.sh                       # simulated demo, opens the UI
#   ./run-local.sh --real --repo /path/to/HattanMedicalHistory --open-pr
#
# Dashboard: http://127.0.0.1:8787
set -euo pipefail
export PYTHONIOENCODING=utf-8
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT="$DIR/agent/loop_agent.py"

MODE_REAL=0; REPO="${REPO_PATH:-}"; OPENPR=""; STEP="${WARN_STEP:-10}"
SIMRUNS=8; SPEED=2; PORT="${UI_PORT:-8787}"; MODEL="${LOOP_MODEL:-glm-5.2:cloud}"
while [ $# -gt 0 ]; do case "$1" in
  --real) MODE_REAL=1;;
  --repo) REPO="$2"; shift;;
  --open-pr) OPENPR="--open-pr";;
  --step) STEP="$2"; shift;;
  --sim-runs) SIMRUNS="$2"; shift;;
  --speed) SPEED="$2"; shift;;
  --port) PORT="$2"; shift;;
  --model) MODEL="$2"; shift;;
  *) echo "unknown arg: $1" >&2; exit 2;;
esac; shift; done

if [ "$MODE_REAL" = "1" ]; then
  [ -n "$REPO" ] || { echo "--real requires --repo <path>"; exit 2; }
  exec python "$AGENT" --repo "$REPO" --step "$STEP" --model "$MODEL" --port "$PORT" --keep-alive $OPENPR
else
  exec python "$AGENT" --simulate --sim-runs "$SIMRUNS" --speed "$SPEED" --step "$STEP" --port "$PORT" --keep-alive
fi
