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

from agent.confluence_flow import run_confluence_comment
from agent.request_flow import run_request
from agent.router_flow import run_github_event, run_health_check, run_router

logger = logging.getLogger(__name__)

# Kept in step with pyproject.toml and metadata.json by scripts/publish.py, and
# asserted by tests/test_version_agreement.py. A constant rather than a file
# read because a workflow may not read files.
AGENT_VERSION = "0.1.53"


async def _dispatch(agent_name: str, payload: dict[str, Any], **options: Any) -> Any:
    """Invoke one child agent as a Temporal child workflow, and await it.

    Synchronous on purpose: whoever waits for the result must be whoever posts
    the reply, or "exactly one reply" stops being structural.

    Only ever on the agent's own task queue (2026-10-15): resolved by name
    alone, an agent registered by another team under the same name
    ("planning_agent") could take the request — with its own code graph and
    repositories. A missing queue is a configuration error, said plainly.
    """
    if not str(options.get("task_queue") or "").strip():
        raise ValueError(
            f"No task queue is configured for {agent_name}: set its ORCH_TASK_QUEUE_* "
            "setting (<agent id>-task-queue). Agents are only called on their own queue, "
            "never by name alone."
        )
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

    if payload.get("confluence_poll") or payload.get("github_poll"):
        # Polling was removed (2026-10-16): Confluence comments and GitHub events
        # arrive by webhook only. A schedule left on gets this answer, not a run.
        logger.warning("mode : poll — removed; switch this Aetherion Schedule off")
        return {
            "status": "ignored",
            "reason": "Polling was removed: Confluence comments and GitHub events now "
            "arrive by webhook. Switch this Aetherion Schedule off.",
        }

    if payload.get("confluence_comment_id"):
        # Pushed by the Confluence webhook: one comment on a TDD page, as it was posted.
        logger.info(
            "mode : confluence comment | page : %s | comment : %s",
            payload.get("confluence_page_id"),
            payload.get("confluence_comment_id"),
        )
        return await run_confluence_comment(
            toolExecutor.execute,
            _dispatch,
            str(payload.get("confluence_page_id") or ""),
            str(payload["confluence_comment_id"]),
        )

    if payload.get("github_event"):
        # Pushed by the GitHub webhook: a merge or a PR comment, as it happened.
        logger.info(
            "mode : github event | %s %s | %s #%s",
            payload.get("github_event"),
            payload.get("github_action"),
            payload.get("github_repo"),
            payload.get("pr_number"),
        )
        return await run_github_event(toolExecutor.execute, _dispatch, payload)

    if str(payload.get("requirement") or "").strip() and not payload.get("issue_key"):
        # A request typed on this agent's form: breakdown PDF, review, design,
        # pull request — nothing written to Jira (agent/request_flow.py).
        logger.info("mode : request from the form")
        return await run_request(payload, toolExecutor.execute, _dispatch)

    logger.info(
        "mode : webhook | issue_key : %s | comment_id : %s",
        payload.get("issue_key"),
        payload.get("comment_id"),
    )
    return await run_router(payload, toolExecutor.execute, _dispatch)
