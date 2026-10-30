"""Agent A — JiraRequirementReview (draft only; never posts).

Thin Aetherion binding: delegates to the pure ``run_review`` flow, passing the
SDK's ``toolExecutor.execute`` and the workflow id. Keeping the logic in
``review_flow`` keeps this deterministic orchestrator unit-testable without the SDK.
"""

from __future__ import annotations

import logging
from typing import Any

from aetherion_sdk import agent, toolExecutor

from agent.delegated import run_delegated
from agent.review_flow import run_review

logger = logging.getLogger(__name__)


def _workflow_id() -> str | None:
    try:
        from temporalio import workflow

        return workflow.info().workflow_id
    except Exception:
        return None


@agent(name="JiraRequirementReview")
async def JiraRequirementReview(payload: dict[str, Any]) -> list[dict]:
    """Review one issue, or a JQL batch.

    Two modes, one implementation. ``mode="delegated"`` is the router's entry
    point: it returns the shared result contract and writes NOTHING to Jira, so
    the router can post exactly one comment. Every other caller — the UI form,
    the CLI, the API — behaves exactly as before.

    The mode is a flag rather than a second agent so the review itself cannot
    drift between the two paths.
    """
    if str(payload.get("mode") or "").strip().lower() == "delegated":
        logger.info(
            "mode : delegated | issue_key : %s | run_id : %s",
            payload.get("issue_key"),
            payload.get("run_id"),
        )
        result = await run_delegated(
            payload,
            toolExecutor.execute,
            run_review=run_review,
            workflow_id=_workflow_id(),
        )
        # A list, because the agent's return type is a list of results and the
        # direct path returns one entry per reviewed issue.
        return [result]

    return await run_review(payload, toolExecutor.execute, workflow_id=_workflow_id())
