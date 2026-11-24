"""Paths and settings, in one place.

Importing this module also loads ``scripts/local_run.py``, which reads
``.env.local`` and Orchestration's ``.env`` (the shell wins) and installs the
local memory and storage — the same set-up the terminal commands use.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

STUDIO = Path(__file__).resolve().parents[2]  # delivery-studio/
ROOT = STUDIO.parent  # the JiraAgent folder
FRONTEND = STUDIO / "frontend"
OUTPUT = ROOT / "local-output"
UPLOADS = ROOT / ".local-uploads"
DOCS_EXPORT = ROOT / "docs-export"  # Task Creation's breakdown PDFs
LOGS = ROOT / ".local-logs"
STATE = OUTPUT / "studio"
RUNS_FILE = STATE / "runs.json"

sys.path.insert(0, str(ROOT / "scripts"))
import local_run  # noqa: E402,F401  (loads the env files; adds Orchestration's src to the path)

TEMPORAL_UI = os.environ.get(
    "STUDIO_TEMPORAL_UI", "https://wf.sbox.aetherion.io/ui/namespaces/test/workflows"
)
GITHUB_PROBE = os.environ.get("STUDIO_GITHUB_PROBE", "https://github.asurint.com/api/v3/meta")
DEFAULT_PORT = int(os.environ.get("STUDIO_PORT", "8780"))
MAX_UPLOAD = 25 * 1024 * 1024

# Each agent's folder -> the name shown on the page.
AGENTS = {
    "jira-orchestration": "Orchestration",
    "jira-requirement-review": "Requirement Review",
    "planning-agent": "Planning",
    "coding-agent": "Coding",
    "jira-task-creation": "Task Creation",
    "local-agents": "Local flow",
}
# A flow step -> the agent that does it.
STEP_AGENT = {
    "review": "jira-requirement-review",
    "tasks": "jira-task-creation",
    "plan": "planning-agent",
    "code": "coding-agent",
}
CONTAINERS = ("planning-neo4j", "planning-s3")


def jira() -> tuple[str, tuple[str, str]]:
    """(the Jira site, its basic-auth pair) from the environment."""
    return (
        os.environ.get("JIRA_BASE_URL", "").rstrip("/"),
        (os.environ.get("JIRA_EMAIL", ""), os.environ.get("JIRA_API_TOKEN", "")),
    )


def project() -> str:
    """The first project Orchestration is allowed to work on (KS)."""
    keys = [k.strip() for k in os.environ.get("ORCH_ALLOWED_PROJECT_KEYS", "").split(",")]
    return next((k.upper() for k in keys if k), "")
