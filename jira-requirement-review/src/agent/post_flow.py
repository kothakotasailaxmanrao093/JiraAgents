"""Orchestration logic for the post agent — pure, SDK-free.

Posts a human-approved/edited comment to Jira. Accepts either the approved
markdown text (preferred) or the structured findings (re-rendered deterministically).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

logger = logging.getLogger(__name__)

Execute = Callable[..., Awaitable[dict]]


async def run_post(
    payload: dict[str, Any],
    execute: Execute,
    workflow_id: str | None = None,
) -> list[dict]:
    issue_key = str(payload.get("issue_key") or "").strip()
    if not issue_key:
        return [{"status": "error", "error": "issue_key is required"}]

    comment_markdown = payload.get("comment_markdown")
    findings = payload.get("findings")
    has_markdown = bool(comment_markdown and str(comment_markdown).strip())
    if not has_markdown and not findings:
        return [
            {
                "status": "error",
                "issue_key": issue_key,
                "error": "no_content",
                "message": "Provide comment_markdown (approved text) or findings to post.",
            }
        ]

    logger.info("Posting review comment: issue=%s wf=%s", issue_key, workflow_id)
    result = await execute(
        "post_review_comment",
        issue_key,
        comment_markdown,
        findings,
        workflow_id,
        start_to_close_timeout=timedelta(minutes=1),
    )

    if not result.get("posted"):
        return [
            {
                "status": "completed_with_warnings",
                "issue_key": issue_key,
                "post_error": result.get("error"),
                "message": f"Failed to post comment to {issue_key}: {result.get('error')}",
            }
        ]
    return [
        {
            "status": "success",
            "issue_key": issue_key,
            "comment_id": result.get("comment_id"),
            "comment_url": result.get("comment_url"),
            "message": f"Posted review comment to {issue_key}.",
        }
    ]
