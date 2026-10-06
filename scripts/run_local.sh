#!/usr/bin/env bash
# Run all four agents on this laptop (2026-10-03).
#
# Each agent's worker serves a queue of its own (scripts/local_worker.py), and
# the router is pointed at the children's local queues, so a run started here
# stays here. Model calls go to OpenAI with the key in .env.local (git-ignored,
# never packed into an agent); Neo4j and S3 for Planning run in Docker.
#
#   scripts/run_local.sh          start all four; Ctrl+C stops them
#   scripts/run_local.sh --jira   only the three Jira agents (router, Task Creation,
#                                 Review): no Planning, no Docker needed
#   scripts/run_local.sh --jira --form
#                                 …and open the Aetherion test form for each one in
#                                 the browser: router :8765, Task Creation :8766,
#                                 Review :8767 (logs: .local-logs/form-<agent>.log)
#   scripts/run_local.sh --poll   …and check GitHub every minute (Planning gets new
#                                 merges and PR comments; log: .local-logs/github-poll.log)
#   logs:  .local-logs/<agent>.log
#   try it: scripts/ask_local.py --health | ISSUE-KEY COMMENT-ID
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS=(planning-agent jira-task-creation jira-requirement-review jira-orchestration)
JIRA_ONLY=0; POLL=0; FORM=0
for arg in "$@"; do
  case "$arg" in
    --jira) JIRA_ONLY=1 ;;
    --poll) POLL=1 ;;
    --form) FORM=1 ;;
    *) echo "usage: run_local.sh [--jira] [--poll] [--form]"; exit 2 ;;
  esac
done
if [ "$JIRA_ONLY" = 1 ]; then
  [ "$POLL" = 1 ] && { echo "error: --poll hands GitHub events to Planning; drop --jira to use it"; exit 2; }
  AGENTS=(jira-task-creation jira-requirement-review jira-orchestration)
fi

if [ -f "$ROOT/.env.local" ]; then
  set -a; . "$ROOT/.env.local"; set +a
else
  echo "warning: no .env.local — model calls will fail (copy .env.local.example)"
fi
[ "$JIRA_ONLY" = 1 ] || for c in planning-neo4j planning-s3; do
  docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c" || echo "warning: Docker container $c is not running"
done
for agent in "${AGENTS[@]}"; do
  [ -x "$ROOT/$agent/.venv/bin/python" ] || { echo "error: $agent has no .venv — run 'uv sync' in it"; exit 1; }
done

eval "$("$ROOT/jira-orchestration/.venv/bin/python" "$ROOT/scripts/local_worker.py" --exports)"

LOGS="$ROOT/.local-logs"; mkdir -p "$LOGS"
pids=()
for agent in "${AGENTS[@]}"; do
  (cd "$ROOT/$agent" && exec .venv/bin/python "$ROOT/scripts/local_worker.py" "$agent") \
    >"$LOGS/$agent.log" 2>&1 &
  pids+=($!)
  echo "started $agent  (log: .local-logs/$agent.log)"
done
if [ "$POLL" = 1 ]; then
  sleep 20  # let the workers connect first
  (cd "$ROOT/jira-orchestration" && exec .venv/bin/python "$ROOT/scripts/poll_github_local.py") \
    >"$LOGS/github-poll.log" 2>&1 &
  pids+=($!)
  echo "started the GitHub check every minute  (log: .local-logs/github-poll.log)"
fi
if [ "$FORM" = 1 ]; then
  sleep 10  # let the workers connect first
  # A form starts its agent on that agent's local queue, like the router does.
  export AETHERION_AGENT_TASK_QUEUE_MAP="$("$ROOT/jira-orchestration/.venv/bin/python" -c \
    "import json, sys; sys.path.insert(0, '$ROOT/scripts'); from local_worker import local_agent_map; print(json.dumps(local_agent_map()))")"
  port=8765
  for agent in jira-orchestration jira-task-creation jira-requirement-review; do
    (cd "$ROOT/$agent" && exec .venv/bin/aetherion test --port "$port") >"$LOGS/form-$agent.log" 2>&1 &
    pids+=($!)
    echo "form for $agent: http://127.0.0.1:$port"
    port=$((port + 1))
  done
fi
trap 'echo; echo "stopping…"; kill "${pids[@]}" 2>/dev/null; wait; echo stopped' INT TERM
echo "${#AGENTS[@]} agents are running. Ctrl+C stops them."
wait
