#!/usr/bin/env bash
# Start the Delivery Studio: the agents on this laptop, then the UI.
#
#   delivery-studio/start.sh              every agent + the UI, and open the browser
#   delivery-studio/start.sh --ui-only    only the UI (the agents are already running)
#   delivery-studio/start.sh --no-open    don't open the browser
#   delivery-studio/start.sh --port 8790  another port (default 8780)
#
# Ctrl+C stops what this started. delivery-studio/stop.sh stops everything.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PYTHON="$ROOT/jira-orchestration/.venv/bin/python"
UI_ONLY=0; NO_OPEN=0; PORT="${STUDIO_PORT:-8780}"
while [ $# -gt 0 ]; do
  case "$1" in
    --ui-only) UI_ONLY=1 ;;
    --no-open) NO_OPEN=1 ;;
    --port) PORT="${2:-}"; [ -n "$PORT" ] || { echo "--port needs a number"; exit 2; }; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)"; exit 2 ;;
  esac
  shift
done
[ -x "$PYTHON" ] || { echo "error: no $PYTHON — run 'uv sync' in jira-orchestration"; exit 1; }
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "error: port $PORT is in use — the studio may already be running (delivery-studio/stop.sh)"
  exit 1
fi
export STUDIO_PORT="$PORT"

if [ "$UI_ONLY" = 0 ]; then
  # run_local.sh starts every agent's worker and, with --studio, this UI.
  args=(--studio)
  [ "$NO_OPEN" = 1 ] && args+=(--no-open)
  exec "$ROOT/scripts/run_local.sh" "${args[@]}"
fi

mkdir -p "$ROOT/.local-logs"
echo "Delivery Studio: http://127.0.0.1:$PORT  (log: .local-logs/studio.log)"
if [ "$NO_OPEN" != 1 ]; then (sleep 3; open "http://127.0.0.1:$PORT" 2>/dev/null || true) & fi
exec "$PYTHON" "$HERE/backend/main.py" --port "$PORT" 2>&1 | tee -a "$ROOT/.local-logs/studio.log"
