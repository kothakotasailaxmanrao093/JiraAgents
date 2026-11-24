#!/usr/bin/env bash
# Stop the Delivery Studio and every local agent worker and form.
#
#   delivery-studio/stop.sh            everything
#   delivery-studio/stop.sh --ui-only  only the UI; the agents keep running
set -uo pipefail
patterns=("delivery-studio/backend/main.py")
if [ "${1:-}" != "--ui-only" ]; then
  patterns+=("scripts/run_local.sh" "scripts/local_worker.py" "scripts/local_form.py"
             "scripts/watch_jira_local.py")
fi
stopped=0
for p in "${patterns[@]}"; do
  if pkill -f "$p" 2>/dev/null; then stopped=$((stopped + 1)); echo "stopped: $p"; fi
done
[ "$stopped" = 0 ] && echo "nothing was running"
exit 0
