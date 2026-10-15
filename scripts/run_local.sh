#!/usr/bin/env bash
# Run all five agents on this laptop (2026-10-03; Coding 2026-10-08).
#
# Each agent's worker serves a queue of its own (scripts/local_worker.py), and
# the router is pointed at the children's local queues, so a run started here
# stays here. Model calls go to OpenAI with the key in .env.local (git-ignored,
# never packed into an agent); Neo4j and S3 for Planning and Coding run in
# Docker (scripts/local_infra.py reads their passwords from the containers).
#
#   scripts/run_local.sh          start all five; Ctrl+C stops them
#   scripts/run_local.sh --form   …and open the Aetherion test form for each one:
#                                 router :8765, Task Creation :8766, Review :8767,
#                                 Planning :8768, Coding :8769
#   scripts/run_local.sh --jira   only the three Jira agents (router, Task Creation,
#                                 Review): no Planning or Coding, no Docker needed;
#                                 a build is followed by a review at most
#   scripts/run_local.sh --jira --form
#                                 …and open the Aetherion test form for each one in
#                                 the browser: router :8765, Task Creation :8766,
#                                 Review :8767 (logs: .local-logs/form-<agent>.log)
#   scripts/run_local.sh --poll   …and listen, as the published router would: new
#                                 "@<keyword>" Jira comments (every 20 s; the webhook's job
#                                 on Aetherion), comments on the TDD pages in Confluence and
#                                 GitHub merges (every minute). Logs: .local-logs/jira-watch.log,
#                                 .local-logs/github-poll.log
#   scripts/run_local.sh --jira --form --site friend
#                                 …against another Jira site: the settings in
#                                 .env.site.friend (git-ignored; template:
#                                 .env.site.example) win over the agents' .env
#   scripts/run_local.sh --jira --form --follow
#                                 …and show the agents' readable log lines in this
#                                 terminal (each block headed by its log file)
#   scripts/run_local.sh --form --keyword AetherionLocal
#                                 …answer "@AetherionLocal build" only: the published
#                                 router (same Jira webhook) ignores those comments
#   logs:  .local-logs/<agent>.log
#   try it: scripts/ask_local.py --health | ISSUE-KEY COMMENT-ID
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS=(planning-agent coding-agent jira-task-creation jira-requirement-review jira-orchestration)
JIRA_ONLY=0; POLL=0; FORM=0; FOLLOW=0; SITE=""; KEYWORD=""
USAGE="usage: run_local.sh [--jira] [--poll] [--form] [--follow] [--site NAME] [--keyword NAME]"
while [ $# -gt 0 ]; do
  case "$1" in
    --jira) JIRA_ONLY=1 ;;
    --poll) POLL=1 ;;
    --form) FORM=1 ;;
    --follow) FOLLOW=1 ;;
    --site) SITE="${2:-}"; [ -n "$SITE" ] || { echo "$USAGE"; exit 2; }; shift ;;
    --keyword) KEYWORD="${2:-}"; [ -n "$KEYWORD" ] || { echo "$USAGE"; exit 2; }; shift ;;
    *) echo "$USAGE"; exit 2 ;;
  esac
  shift
done
if [ -n "$SITE" ]; then
  SITE_FILE="$ROOT/.env.site.$SITE"
  [ -f "$SITE_FILE" ] || { echo "error: no $SITE_FILE — copy .env.site.example and fill it in"; exit 2; }
  # The GitHub check keeps its memory in a BGV project property; not on another site.
  [ "$POLL" = 1 ] && { echo "error: --poll runs against your own site only; drop --site to use it"; exit 2; }
fi
if [ "$JIRA_ONLY" = 1 ]; then
  [ "$POLL" = 1 ] && { echo "error: --poll hands GitHub events to Planning; drop --jira to use it"; exit 2; }
  AGENTS=(jira-task-creation jira-requirement-review jira-orchestration)
fi

if [ -f "$ROOT/.env.local" ]; then
  set -a; . "$ROOT/.env.local"; set +a
else
  echo "warning: no .env.local — model calls will fail (copy .env.local.example)"
fi
if [ -n "$SITE" ]; then
  # After .env.local, so the site's values win; the workers keep what the shell has.
  set -a; . "$SITE_FILE"; set +a
  echo "Jira site: ${JIRA_BASE_URL:-?} (from .env.site.$SITE)"
fi
[ "$JIRA_ONLY" = 1 ] || for c in planning-neo4j planning-s3; do
  docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c" || echo "warning: Docker container $c is not running"
done
if [ "$JIRA_ONLY" = 1 ]; then
  # Planning and Coding are not running: a build, or a request from the form,
  # goes on to a review at most.
  export ORCH_BUILD_PIPELINE=review
fi
echo "after a build: ${ORCH_BUILD_PIPELINE:-(as in jira-orchestration/.env)}"
if [ -n "$KEYWORD" ]; then
  # Router and Task Creation both check the mention; Review is only ever asked by the router.
  export ORCH_TRIGGER_KEYWORD="$KEYWORD" TASK_TRIGGER_KEYWORD="$KEYWORD"
  echo "answering @$KEYWORD only (the published agents keep answering @Aetherion)"
fi
for agent in "${AGENTS[@]}"; do
  [ -x "$ROOT/$agent/.venv/bin/python" ] || { echo "error: $agent has no .venv — run 'uv sync' in it"; exit 1; }
done

eval "$("$ROOT/jira-orchestration/.venv/bin/python" "$ROOT/scripts/local_worker.py" --exports)"
# Review's Word reports: saved here on a laptop run (no object storage locally).
export REVIEW_REPORT_DIR="$ROOT/docs-export/review-reports"
# Task Creation's PDFs (emailed, or of tickets created): a copy kept here too.
export TASK_DOCUMENT_DIR="$ROOT/docs-export/task-creation"
# Same input, same plan: Task Creation keeps the plans it wrote here (24 h).
export TASK_PLAN_CACHE_DIR="$ROOT/.local-cache/plans"
# A request from the router's form: a copy of each technical design, to open.
export ORCH_DOCUMENT_DIR="$ROOT/docs-export/pipeline"

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
  echo "started the GitHub and Confluence checks every minute  (log: .local-logs/github-poll.log)"
  (cd "$ROOT/jira-orchestration" && exec .venv/bin/python "$ROOT/scripts/watch_jira_local.py") \
    >"$LOGS/jira-watch.log" 2>&1 &
  pids+=($!)
  echo "started the Jira comment watcher every 20 s  (log: .local-logs/jira-watch.log)"
fi
if [ "$FORM" = 1 ]; then
  sleep 10  # let the workers connect first
  # A form starts its agent on that agent's local queue, like the router does.
  export AETHERION_AGENT_TASK_QUEUE_MAP="$("$ROOT/jira-orchestration/.venv/bin/python" -c \
    "import json, sys; sys.path.insert(0, '$ROOT/scripts'); from local_worker import local_agent_map; print(json.dumps(local_agent_map()))")"
  # One port per agent, the same whichever agents run.
  for entry in jira-orchestration:8765 jira-task-creation:8766 jira-requirement-review:8767 \
               planning-agent:8768 coding-agent:8769; do
    agent="${entry%%:*}"; port="${entry##*:}"
    case " ${AGENTS[*]} " in *" $agent "*) ;; *) continue ;; esac
    # The `aetherion test` form, but picked files are kept on this laptop
    # (.local-uploads/) instead of uploaded to the hosted platform.
    (cd "$ROOT/$agent" && exec .venv/bin/python "$ROOT/scripts/local_form.py" --port "$port") \
      >"$LOGS/form-$agent.log" 2>&1 &
    pids+=($!)
    echo "form for $agent: http://127.0.0.1:$port"
  done
fi
if [ "$FOLLOW" = 1 ]; then
  # Only what a person reads: no worker-poll retries, raw HTTP GETs or the
  # stdlib logger's duplicate of every line — except the lines only the stdlib
  # logger writes: each step of a request, the review's verdict, what was read.
  logs=()
  for agent in "${AGENTS[@]}"; do logs+=("$LOGS/$agent.log"); done
  [ "$POLL" = 1 ] && logs+=("$LOGS/jira-watch.log" "$LOGS/github-poll.log")
  keep='request_flow|router_flow|pipeline_flow|command_flow|confluence_flow|analyze_requirement_tool|gather_documents_tool'
  tail -n 0 -F "${logs[@]}" 2>/dev/null \
    | awk -v keep="$keep" '/temporalio|HTTP Request: GET/ {next}
        /^(INFO|WARNING):/ && $0 !~ keep {next}
        {print; fflush()}' &
  pids+=($!)
fi
trap 'echo; echo "stopping…"; kill "${pids[@]}" 2>/dev/null; wait; echo stopped' INT TERM
echo "${#AGENTS[@]} agents are running. Ctrl+C stops them."
wait
