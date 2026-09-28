"""@tool: build a consolidated Word (.docx) review report and upload it.

Thin activity shim — all rendering logic lives in ``review.report`` (pure,
unit-tested); this module only wires that output to object storage, mirroring
``render_review_tool``'s split between pure rendering and I/O.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from aetherion_sdk import tool
from dotenv import load_dotenv

from config import resolve_team_id
from review.report import aggregate_report_metrics, build_docx_report, build_report_email_html

load_dotenv()
logger = logging.getLogger(__name__)

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@tool(name="build_review_report")
async def build_review_report(
    label: str,
    issue_results: list[dict[str, Any]],
    team_id: str | None = None,
) -> dict[str, Any]:
    """
    Build the consolidated Word (.docx) review report, upload it to object
    storage, and produce an HTML email summary.

    Args:
        label: Issue key or JQL query used (for the filename and title).
        issue_results: Per-issue result dicts produced by the review flow.
        team_id: Storage bucket override; falls back to ``TENANT_ID``.

    Returns:
        {"s3_key": str | None, "extension": "docx", "metrics": {...},
         "email_html": str, "error": str}  # error only on upload failure
    """
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    metrics = aggregate_report_metrics(issue_results)

    docx_bytes = build_docx_report(label, issue_results, metrics, generated_at)
    email_html = build_report_email_html(label, issue_results, metrics)

    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    safe_label = "".join(c if c.isalnum() else "_" for c in label)[:60]
    s3_key = f"RequirementReviewReports/{safe_label}_{date_str}.docx"

    bucket_name = resolve_team_id(team_id) or os.environ.get("TENANT_ID")
    if not bucket_name:
        logger.error("No team_id/TENANT_ID set; cannot upload review report to storage.")
        return {
            "s3_key": None,
            "extension": "docx",
            "metrics": metrics,
            "email_html": email_html,
            "error": "TENANT_ID not set",
        }

    try:
        from common_lib.storage.storage_client import storage

        storage.init_client()
        storage.store_object(
            bucket_name=bucket_name,
            object_key=s3_key,
            data=docx_bytes,
            content_type=DOCX_CONTENT_TYPE,
        )
        logger.info("Review report uploaded: %s (%d bytes)", s3_key, len(docx_bytes))
    except Exception as e:
        logger.error("Failed to upload review report: %s", e, exc_info=True)
        return {
            "s3_key": None,
            "extension": "docx",
            "metrics": metrics,
            "email_html": email_html,
            "error": str(e),
        }

    return {
        "s3_key": s3_key,
        "extension": "docx",
        "metrics": metrics,
        "email_html": email_html,
        "error": None,
    }
