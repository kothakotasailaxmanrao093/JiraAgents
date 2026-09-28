"""Run the work-breakdown agent against one Jira issue, without Temporal.

A local harness for testing the webhook flow before the agent is published.
It drives the real agent function and the real tools — the only thing it
replaces is Temporal's ``toolExecutor.execute``, which normally dispatches each
tool onto a task queue. Everything else (Jira reads, Jira writes, Gmail, the
decomposer) is exactly what runs in production.

Usage:
    python scripts/run_issue.py TT2-140              # webhook mode
    python scripts/run_issue.py TT2-140 --force      # ignore keyword / processed label
    python scripts/run_issue.py TT2-140 --dry-run    # decompose, write nothing
    python scripts/run_issue.py --text "Track fuel spend per vehicle." --project TT2

Once `aetherion run` is available, the same run is:
    aetherion agent JiraTaskCreation '{"issue_key": "TT2-140"}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from src.agent import agent as agent_module  # noqa: E402
from src.agent.agent import JiraTaskCreation  # noqa: E402
from src.tools import tools as t  # noqa: E402

TOOLS = {
    "read_jira_issue": t.read_jira_issue,
    "inspect_jira_context": t.inspect_jira_context,
    "validate_requirement": t.validate_requirement,
    "generate_work_breakdown": t.generate_work_breakdown,
    "create_jira_issues": t.create_jira_issues,
    "notify_email": t.notify_email,
    "report_to_issue": t.report_to_issue,
}


def build_payload(args: argparse.Namespace) -> dict:
    payload: dict = {"create_in_jira": not args.dry_run}
    if args.issue_key:
        payload["issue_key"] = args.issue_key
        if args.force:
            payload["force"] = True
    else:
        payload["requirement"] = args.text
        payload["project_key"] = args.project
    if args.sprint:
        payload["placement"] = "current_sprint"
    if args.epic:
        payload["existing_epic_key"] = args.epic
    return payload


async def main(payload: dict) -> dict:
    calls: list[str] = []

    async def execute(name: str, *call_args, **_kwargs):
        calls.append(name)
        return await TOOLS[name](*call_args)

    agent_module.toolExecutor.execute = execute
    result = await JiraTaskCreation.fn(payload)

    print("\n" + "=" * 72)
    print("TOOLS CALLED:", " -> ".join(calls))
    print("=" * 72)
    print(result.get("summary_text", ""))
    print("=" * 72)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue_key", nargs="?", help="Jira issue key, e.g. TT2-140")
    parser.add_argument("--text", help="Manual mode: the requirement text")
    parser.add_argument("--project", help="Manual mode: the Jira project key")
    parser.add_argument("--force", action="store_true", help="Ignore keyword and processed label")
    parser.add_argument("--dry-run", action="store_true", help="Decompose but write nothing")
    parser.add_argument("--sprint", action="store_true", help="Place stories in the active sprint")
    parser.add_argument("--epic", help="Add stories under this existing Epic key")
    parser.add_argument("--json", action="store_true", help="Also print the full result as JSON")
    args = parser.parse_args()

    if not args.issue_key and not args.text:
        parser.error("give an issue key (webhook mode) or --text with --project (manual mode)")
    if args.text and not args.project:
        parser.error("--text also needs --project")

    outcome = asyncio.run(main(build_payload(args)))
    if args.json:
        print(json.dumps(outcome, indent=2, default=str))
    sys.exit(0 if outcome.get("status") != "JIRA_CREATION_FAILED" else 1)
