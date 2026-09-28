"""Agent B — PostJiraReviewComment (posts the approved/edited comment).

Thin Aetherion binding: delegates to the pure ``run_post`` flow.
"""

from __future__ import annotations

import logging
from typing import Any

from aetherion_sdk import agent, toolExecutor

from agent.post_flow import run_post

logger = logging.getLogger(__name__)


def _workflow_id() -> str | None:
    try:
        from temporalio import workflow

        return workflow.info().workflow_id
    except Exception:
        return None


@agent(name="PostJiraReviewComment")
async def PostJiraReviewComment(payload: dict[str, Any]) -> list[dict]:
    return await run_post(payload, toolExecutor.execute, workflow_id=_workflow_id())
