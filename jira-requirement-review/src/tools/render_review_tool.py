"""@tool: render findings into an editable markdown draft + an ADF preview.

Pure (no network); kept as an activity for consistency and so the workflow stays
free of rendering work. Produces both the human-editable markdown (what the user
reviews) and an ADF preview.
"""

from __future__ import annotations

import logging
from typing import Any

from aetherion_sdk import tool

from review.adf import render_review_to_adf, render_review_to_markdown
from review.models import Readiness
from review.parser import parse_findings

logger = logging.getLogger(__name__)


@tool(name="render_review")
async def render_review(
    issue_key: str,
    findings: list[dict[str, Any]],
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    review = parse_findings({"findings": findings})
    grouped = review.grouped()
    by_category = {ft.value: len(items) for ft, items in grouped.items()}
    readiness_model = Readiness.model_validate(readiness) if readiness else None

    markdown = render_review_to_markdown(review, issue_key, readiness=readiness_model)
    adf = render_review_to_adf(review, issue_key, readiness=readiness_model)

    logger.info(
        "render_review: %d finding(s) for %s, categories=%s",
        len(review.findings),
        issue_key,
        by_category,
    )
    return {
        "comment_markdown": markdown,
        "comment_adf": adf,
        "by_category": by_category,
        "finding_count": len(review.findings),
    }
