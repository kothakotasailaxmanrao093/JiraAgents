#!/usr/bin/env bash
# Follow the logs while a run goes.
#
#   delivery-studio/logs.sh             the UI and every agent, readable lines only
#   delivery-studio/logs.sh planning    one agent: studio, orchestration, review,
#                                       planning, coding, task-creation
set -euo pipefail
LOGS="$(cd "$(dirname "$0")/.." && pwd)/.local-logs"
case "${1:-all}" in
  all) files=("$LOGS/studio.log" "$LOGS/jira-orchestration.log" "$LOGS/jira-requirement-review.log"
              "$LOGS/planning-agent.log" "$LOGS/coding-agent.log") ;;
  studio) files=("$LOGS/studio.log") ;;
  orchestration) files=("$LOGS/jira-orchestration.log") ;;
  review) files=("$LOGS/jira-requirement-review.log") ;;
  planning) files=("$LOGS/planning-agent.log") ;;
  coding) files=("$LOGS/coding-agent.log") ;;
  task-creation) files=("$LOGS/jira-task-creation.log") ;;
  *) sed -n '2,6p' "$0"; exit 2 ;;
esac
# No polling noise or raw HTTP lines; the terminal colours are kept.
tail -n 20 -F "${files[@]}" 2>/dev/null | grep --line-buffered -vE "HTTP Request: GET|temporalio"
