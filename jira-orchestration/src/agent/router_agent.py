"""The router agent — a thin Aetherion binding over the pure routing sequence.

Everything decidable lives in ``router_flow``; this supplies the SDK's executors
and nothing else. That split is what lets the whole router be tested without
Temporal, without Jira and without a model — and the properties worth testing
("exactly one reply on every path, including failures") are precisely the ones
that are hard to observe through a live worker.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from aetherion_sdk import agent, agentExecutor, toolExecutor

from agent.router_flow import run_health_check, run_router

logger = logging.getLogger(__name__)

# Kept in step with pyproject.toml and metadata.json by scripts/publish.py, and
# asserted by tests/test_version_agreement.py. A constant rather than a file
# read because a workflow may not read files.
AGENT_VERSION = "0.1.24"


async def _dispatch(agent_name: str, payload: dict[str, Any], **options: Any) -> Any:
    """Invoke one child agent as a Temporal child workflow, and await it.

    Synchronous on purpose: whoever waits for the result must be whoever posts
    the reply, or "exactly one reply" stops being structural. See
    ../docs/AGENT_TO_AGENT.md.
    """
    return await agentExecutor.execute(agent_name, payload, **options)


@agent()
async def JiraOrchestration(payload: Dict[str, Any]) -> dict:
    """One Jira comment in, at most one Jira comment out.

    Triggered by a single ``comment_created`` webhook for the whole system. It
    filters, de-duplicates, classifies, dispatches to exactly one agent, and
    posts the one reply.

    It routes. It never gathers deep context and never creates Jira issues.
    """
    if payload.get("health_check"):
        # Bypasses ingress entirely — no issue_key needed, nothing posted to
        # Jira. See run_health_check for why this has to be a workflow-side
        # branch rather than a separate @tool.
        logger.info("mode : health_check")
        return await run_health_check(_dispatch, toolExecutor.execute)

    logger.info(
        "mode : webhook | issue_key : %s | comment_id : %s",
        payload.get("issue_key"),
        payload.get("comment_id"),
    )
    return await run_router(payload, toolExecutor.execute, _dispatch)
