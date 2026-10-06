"""Run one agent's worker on its OWN local task queue (2026-10-03).

``aetherion run`` puts every local worker on this laptop on one shared queue
(``aetherion_sdk.task_queue.local_task_queue()``). With four agents running at
once, a job for one agent can be handed to another agent's worker, which does
not have it. This starts the same runtime the CLI does, but on a queue of the
agent's own, so each job reaches the worker that has its code.

Run from the agent's folder, with that agent's Python:

    cd jira-orchestration && .venv/bin/python ../scripts/local_worker.py jira-orchestration

``scripts/run_local.sh`` starts all four this way.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Each folder's agents, by the names other agents start them with.
AGENT_NAMES = {
    "jira-orchestration": ("JiraOrchestration",),
    "jira-task-creation": ("JiraTaskCreation",),
    "jira-requirement-review": ("JiraRequirementReview", "PostJiraReviewComment"),
    "planning-agent": ("planning_agent", "query_agent"),
}
AGENTS = tuple(AGENT_NAMES)


def local_queue(agent: str) -> str:
    """This laptop's queue for one agent: stable, and distinct per agent."""
    from aetherion_sdk.task_queue import local_queue_id

    return f"local-{agent}-{local_queue_id()}"


async def main(agent: str) -> None:
    from aetherion_sdk.config import AetherionSettings
    from aetherion_sdk.runtime import AetherionRuntime

    queue = local_queue(agent)
    # The agent's own settings, as the CLI loads them; the shell's values (the
    # OpenAI key, the local child queues) win over the file's.
    load_dotenv(Path.cwd() / ".env", override=False)
    os.environ["AETHERION_AGENT_TASK_QUEUE"] = queue
    os.environ["AETHERION_TOOL_TASK_QUEUE"] = queue
    # The only switch for model calls to go to OpenAI (shared/llm.py): set here,
    # never in an agent's .env, so a published agent always uses the gateway.
    os.environ["AETHERION_LOCAL_RUN"] = "1"
    # Any agent started from here — a child, a health-check probe — goes to
    # that agent's local worker; tools stay on this worker's own queue.
    agent_map = local_agent_map()
    os.environ["AETHERION_AGENT_TASK_QUEUE_MAP"] = json.dumps(agent_map)
    os.environ["AETHERION_TOOL_TASK_QUEUE_MAP"] = "{}"
    settings = AetherionSettings(
        workflow_task_queue=queue,
        activity_task_queue=queue,
        agent_task_queue_map=agent_map,
        tool_task_queue_map={},
    )
    runtime = AetherionRuntime(settings=settings)
    packages = src_packages(Path.cwd())
    print(f"{agent}: serving local queue {queue} ({', '.join(packages)})", flush=True)
    await runtime.run_workers(packages)


def local_agent_map() -> dict[str, str]:
    """Every local agent's name -> its folder's local queue."""
    return {
        name: local_queue(folder)
        for folder, names in AGENT_NAMES.items()
        for name in names
    }


def src_packages(root: Path) -> list[str]:
    """The agent's packages, found as the CLI finds them: every folder under
    ``src`` with an ``__init__.py``, imported with ``src`` on the path."""
    src = root / "src"
    sys.path.insert(0, str(src))
    return sorted(p.parent.name for p in src.glob("*/__init__.py"))


# The router's settings that name each child's queue (routing/settings.py).
CHILD_QUEUE_SETTINGS = {
    "jira-task-creation": "ORCH_TASK_QUEUE_JIRA_TASK_CREATION",
    "jira-requirement-review": "ORCH_TASK_QUEUE_JIRA_REQUIREMENT_REVIEW",
    "planning-agent": "ORCH_TASK_QUEUE_PLANNING",
}


def exports() -> str:
    """Shell lines pointing the router at the local children's queues."""
    return "\n".join(
        f"export {name}={local_queue(agent)}"
        for agent, name in CHILD_QUEUE_SETTINGS.items()
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["--exports"]:
        print(exports())
    elif len(sys.argv) == 2 and sys.argv[1] in AGENTS:
        asyncio.run(main(sys.argv[1]))
    else:
        sys.exit(f"usage: local_worker.py {{{'|'.join(AGENTS)}}} | --exports")
