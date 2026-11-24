#!/usr/bin/env bash
# One-time setup on a new laptop, from the JiraAgent folder:
#
#   delivery-studio/setup.sh
#
# 1. Planning and Coding: taken from AetherionAgents/asurint (branches
#    sm/planning-agent, sm/coding-agent) into planning-agent/ and coding-agent/
#    when those folders are missing. They are git-ignored here.
# 2. Each agent's Python packages (uv sync).
# 3. The Docker containers Planning needs: Neo4j and local object storage
#    (RustFS), with passwords made here and kept only inside the containers.
# 4. The settings files, copied from their templates when missing — fill them in.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ASURINT="https://github.com/AetherionAgents/asurint.git"
need() { command -v "$1" >/dev/null 2>&1 || { echo "error: $1 is not installed ($2)"; exit 1; }; }
need git "https://git-scm.com"
need uv "brew install uv"
need docker "Docker Desktop"

echo "== 1. Planning and Coding"
for agent in planning-agent coding-agent; do
  if [ -d "$agent" ]; then
    echo "  $agent/ already here — kept"
    continue
  fi
  tmp="$(mktemp -d)"
  git clone -q --depth 1 -b "sm/$agent" "$ASURINT" "$tmp"
  mv "$tmp/$agent" "$agent"
  rm -rf "$tmp"
  echo "  $agent/ taken from asurint sm/$agent"
done

echo "== 2. Python packages"
for agent in jira-orchestration jira-requirement-review jira-task-creation planning-agent coding-agent; do
  (cd "$agent" && uv sync -q) && echo "  $agent: ok"
done

echo "== 3. Docker containers"
docker info >/dev/null 2>&1 || { echo "error: start Docker Desktop first"; exit 1; }
secret() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24; }
if ! docker ps -a --format '{{.Names}}' | grep -qx planning-neo4j; then
  docker run -d --name planning-neo4j --restart unless-stopped -p 7474:7474 -p 7687:7687 \
    -e NEO4J_AUTH="neo4j/$(secret)" neo4j:5.26 >/dev/null
  echo "  planning-neo4j created"
else
  docker start planning-neo4j >/dev/null && echo "  planning-neo4j running"
fi
if ! docker ps -a --format '{{.Names}}' | grep -qx planning-s3; then
  docker run -d --name planning-s3 --restart unless-stopped -p 9000:9000 -p 9001:9001 \
    -e RUSTFS_ACCESS_KEY="$(secret)" -e RUSTFS_SECRET_KEY="$(secret)" \
    -e RUSTFS_CONSOLE_ENABLE=true rustfs/rustfs:latest >/dev/null
  echo "  planning-s3 created"
else
  docker start planning-s3 >/dev/null && echo "  planning-s3 running"
fi
echo "  (the agents read these passwords from the containers; leave NEO4J_* and STORAGE_* blank in .env)"

echo "== 4. Settings files"
copy() { [ -f "$2" ] && echo "  $2 exists — kept" || { cp "$1" "$2"; echo "  $2 made from $1 — FILL IT IN"; }; }
copy .env.local.example .env.local
for agent in jira-orchestration jira-requirement-review jira-task-creation planning-agent coding-agent; do
  [ -f "$agent/.env.template" ] && copy "$agent/.env.template" "$agent/.env"
done
cat <<'EOF'

Done. Before the first run:
  - fill in .env.local (OPENAI_API_KEY) and each agent's .env (Jira, GitHub, CODE_GRAPH_KEY):
    get the values in person or from a password manager — never in chat, never in git;
    CODE_GRAPH_KEY must be the SAME in jira-orchestration and planning-agent;
    coding-agent/.env: CODING_OPEN_PR=true to open draft pull requests, JENKINS_ENABLED=false
  - connect the VPN (the Keystone code is on github.asurint.com)

Then:  delivery-studio/start.sh
EOF
