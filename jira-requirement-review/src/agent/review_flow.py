"""Orchestration logic for the review (draft) agent — pure, SDK-free.

The deterministic step sequence lives here so it can be unit-tested with a fake
``execute`` (no Temporal, no SDK). ``review_agent.py`` is a thin binding that
passes ``toolExecutor.execute`` as ``execute``.

Supports two modes:
  * single issue  — ``issue_key`` is reviewed and a draft/comment is produced.
  * batch (JQL)    — ``jql_override`` resolves to a bounded set of issues, each
    run through the same per-issue pipeline (merged in from the pre-grooming
    clarity agent this feature set was combined from).

In both modes, ``post_to_jira`` (per issue) and ``generate_docx`` (a consolidated
report across whatever was reviewed) are independent, optional delivery steps —
this agent never posts to Jira unless explicitly asked to. Email delivery
(``send_email``) is implemented but TEMPORARILY DISABLED — see the note below.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from config import flag

logger = logging.getLogger(__name__)

# execute(tool_name, *args, start_to_close_timeout=...) -> dict
Execute = Callable[..., Awaitable[dict]]

# Payload keys under which the platform may surface an uploaded transcript file.
# The exact key depends on how the SDK maps a `file` trigger; we accept the
# documented options, the trigger's own name, and the SDK's generic "attachments".
_TRANSCRIPT_KEYS = ("uploaded_files", "transcript_file", "transcript_file_key", "attachments")

# Best-effort notification for the consolidated report, via the Pulsar
# notification service (see tools/send_review_report_email_tool.py). Requires
# NOTIFICATIONS_BASE_URL + JIRA_NOTIFY_EMAILS/JIRA_NOTIFY_USER_IDS configured, and
# REPORT_EMAIL_TEMPLATE registered on the platform's Pulsar instance — the tool
# degrades to {"status": "skipped"/"error"} rather than raising if either is missing.
REPORT_EMAIL_ACTIVITY = "send_review_report_email"
REPORT_EMAIL_TEMPLATE = "jira_requirement_review_report"


def resolve_transcript_file_keys(payload: dict[str, Any]) -> list[str]:
    """Every uploaded file's storage key, from the first payload key that has any.

    The form's upload field takes several files (a transcript and a spec, say);
    the platform sends their keys as a list, or a comma-separated string.
    """
    for key in _TRANSCRIPT_KEYS:
        value = payload.get(key)
        items = value if isinstance(value, list) else str(value or "").split(",")
        keys = [str(item).strip() for item in items if str(item or "").strip()]
        if keys:
            return list(dict.fromkeys(keys))
    return []


async def _review_one_issue(
    issue_key: str,
    execute: Execute,
    *,
    include_parent: bool,
    include_subtasks: bool,
    include_linked_issues: bool,
    include_attachments: bool,
    include_confluence: bool,
    trigger_comment_id: str | None,
    transcript_file_keys: list[str],
    team_id: str | None,
    model_id: str | None,
    post_to_jira: bool,
    workflow_id: str | None,
) -> dict[str, Any]:
    """Run the full context-gather -> analyze -> render -> (optional post) pipeline
    for one issue and return a single structured result dict."""
    ctx = await execute(
        "gather_context",
        issue_key,
        include_parent,
        include_subtasks,
        include_linked_issues,
        include_attachments,
        include_confluence,
        trigger_comment_id,
        transcript_file_keys,
        team_id,
        start_to_close_timeout=timedelta(minutes=2),
    )
    if ctx.get("error"):
        return {
            "status": "error",
            "issue_key": issue_key,
            "error": ctx["error"],
            "message": f"Could not gather context for {issue_key}: {ctx['error']}",
        }
    if not ctx.get("has_requirement"):
        return {
            "status": "error",
            "issue_key": issue_key,
            "error": "no_requirement_text",
            "warnings": ctx.get("warnings", []),
            "documents": ctx.get("documents") or [],
            "target_bundle": ctx.get("target_bundle") or {},
            "message": (
                f"{issue_key} has no description, attachment or linked page that " "could be read."
            ),
        }

    analysis = await execute(
        "analyze_requirement",
        ctx.get("documents", []),
        issue_key,
        ctx.get("issue_summary"),
        model_id,
        bool((ctx.get("target_bundle") or {}).get("is_subtask")),
        start_to_close_timeout=timedelta(minutes=5),
    )
    if analysis.get("error") and not analysis.get("findings"):
        return {
            "status": "completed_with_warnings",
            "issue_key": issue_key,
            "analysis_error": analysis["error"],
            "warnings": ctx.get("warnings", []),
            "message": "Analysis unavailable; no draft generated.",
        }

    readiness = analysis.get("readiness")
    rendered = await execute(
        "render_review",
        issue_key,
        analysis.get("findings", []),
        readiness,
        start_to_close_timeout=timedelta(seconds=30),
    )

    finding_count = rendered.get("finding_count", 0)
    draft = rendered.get("comment_markdown", "")
    findings = analysis.get("findings", [])
    by_category = rendered.get("by_category", {})
    warnings = ctx.get("warnings", [])

    result: dict[str, Any] = {
        "issue_key": issue_key,
        "summary": ctx.get("issue_summary"),
        "finding_count": finding_count,
        "by_category": by_category,
        "findings": findings,
        "readiness": readiness,
        # Open questions the ticket already lists — not repeated as findings.
        "known_questions": analysis.get("known_questions") or [],
        "warnings": warnings,
        # Carried so the delegated path can account for what was read and what
        # was not. Unused by the direct path, which reports through the draft.
        "documents": ctx.get("documents") or [],
        "target_bundle": ctx.get("target_bundle") or {},
        "counts": ctx.get("counts") or {},
    }

    # === Publish-to-Jira path (opt-in via the "Publish to Jira" trigger) ===
    if post_to_jira:
        posted = await execute(
            "post_review_comment",
            issue_key,
            None,  # comment_markdown — let the tool render structured ADF from findings
            findings,
            workflow_id,
            readiness,
            start_to_close_timeout=timedelta(minutes=1),
        )
        if posted.get("posted"):
            result.update(
                status="success",
                posted=True,
                comment_id=posted.get("comment_id"),
                comment_url=posted.get("comment_url"),
                message=(
                    f"Posted review comment to {issue_key} "
                    f"({finding_count} finding(s)). {posted.get('comment_url', '')}"
                ),
            )
            return result
        # Posting failed — fall back to the draft so the user can still copy/paste it.
        result.update(
            status="completed_with_warnings",
            posted=False,
            comment_markdown=draft,
            post_error=posted.get("error"),
            message=(
                f"Could not post to {issue_key}: {posted.get('error')}. "
                f"Draft below — copy it into Jira manually.\n\n"
                f"----- DRAFT COMMENT (copy below) -----\n\n{draft}"
            ),
        )
        return result

    # === Draft-only path (default) ===
    # Jira's per-user comment draft cannot be created via the REST API, so we surface
    # the full ready-to-paste draft here. The user pastes it into the Jira comment box
    # (a private draft only they see) and clicks Comment to publish it for everyone.
    instructions = (
        f"DRAFT review for {issue_key} ({finding_count} finding(s)) — NOT posted to Jira. "
        f"Copy the comment below into the comment box on {issue_key} in Jira; it stays a "
        "private draft only you can see until you click Comment to publish it for everyone."
    )
    result.update(
        status="success",
        posted=False,
        comment_markdown=draft,
        comment_adf=rendered.get("comment_adf"),
        message=f"{instructions}\n\n----- DRAFT COMMENT (copy below) -----\n\n{draft}",
    )
    return result


async def _send_report_email(execute: Execute, context: dict[str, Any]) -> dict[str, Any]:
    """Best-effort email dispatch — never lets a notification problem fail the run."""
    try:
        return await execute(
            REPORT_EMAIL_ACTIVITY,
            REPORT_EMAIL_TEMPLATE,
            context,
            None,
            start_to_close_timeout=timedelta(seconds=30),
        )
    except Exception as e:
        logger.error("Email dispatch failed at activity layer: %s", e)
        return {"status": "error", "error": str(e)}


async def run_review(
    payload: dict[str, Any],
    execute: Execute,
    workflow_id: str | None = None,
) -> list[dict]:
    issue_key = str(payload.get("issue_key") or "").strip()
    jql_override = str(payload.get("jql_override") or "").strip()

    if not issue_key and not jql_override:
        return [
            {
                "status": "error",
                "error": "issue_key_or_jql_required",
                "message": (
                    "Provide 'issue_key' (e.g. ABC-123) or a Sprint/JQL query "
                    "('jql_override', e.g. 'sprint = \"Sprint 42\"')."
                ),
            }
        ]

    common = dict(
        include_parent=flag(payload, "include_parent"),
        include_subtasks=flag(payload, "include_subtasks"),
        include_linked_issues=flag(payload, "include_linked_issues"),
        include_attachments=flag(payload, "include_attachments"),
        include_confluence=flag(payload, "include_confluence"),
        trigger_comment_id=str(payload.get("comment_id") or "").strip() or None,
        transcript_file_keys=resolve_transcript_file_keys(payload),
        team_id=payload.get("team_id"),
        model_id=payload.get("model_id"),
        post_to_jira=flag(payload, "post_to_jira"),
        workflow_id=workflow_id,
    )
    generate_docx = flag(payload, "generate_docx")
    # TEMPORARILY DISABLED: email delivery is not exposed as a trigger (see
    # metadata.json) and payload["send_email"] is ignored regardless of what's
    # sent — Pulsar template/recipient config isn't verified for this agent yet.
    # Re-enable by restoring `bool(payload.get("send_email", False))` once confirmed.
    send_email = False

    if jql_override:
        label = jql_override
        logger.info("Requirement review starting (batch): jql=%s wf=%s", jql_override, workflow_id)
        search = await execute(
            "jira_search",
            jql_override,
            payload.get("max_results"),
            start_to_close_timeout=timedelta(minutes=1),
        )
        if search.get("error"):
            return [
                {
                    "status": "error",
                    "error": search["error"],
                    "message": f"Could not search Jira for '{jql_override}': {search['error']}",
                }
            ]
        issue_keys = search.get("issue_keys") or []
        if not issue_keys:
            return [
                {
                    "status": "error",
                    "error": "no_issues_matched",
                    "message": (
                        f"No Jira issues matched '{jql_override}'. Check the query and "
                        "Jira credentials."
                    ),
                }
            ]
        results = []
        for key in issue_keys:
            results.append(await _review_one_issue(key, execute, **common))
    else:
        label = issue_key
        logger.info("Requirement review starting: issue=%s wf=%s", issue_key, workflow_id)
        results = [await _review_one_issue(issue_key, execute, **common)]

    output: list[dict] = list(results)

    if generate_docx or send_email:
        report = await execute(
            "build_review_report",
            label,
            results,
            common["team_id"],
            start_to_close_timeout=timedelta(minutes=2),
        )
        s3_key = report.get("s3_key")
        if generate_docx and s3_key:
            output.append(
                {
                    "type": "s3_download_link",
                    "title": f"Requirement Review Report — {label}",
                    "file_key": s3_key,
                    "label": "Download Review Report",
                    "extension": report.get("extension", "docx"),
                }
            )

        email_status = "skipped"
        if send_email:
            sent = await _send_report_email(
                execute,
                {
                    "agent_name": "Jira Requirement Review Agent",
                    "label": label,
                    "issue_count": len(results),
                    "metrics": report.get("metrics", {}),
                    "report_html": report.get("email_html", ""),
                    "s3_key": s3_key,
                    "report_extension": report.get("extension", "docx"),
                    "workflow_id": workflow_id,
                },
            )
            email_status = sent.get("status", "unknown")

        metrics = report.get("metrics", {})
        output.append(
            {
                "status": "success" if not report.get("error") else "completed_with_warnings",
                "mode": "batch" if jql_override else "single",
                "label": label,
                "issue_count": len(results),
                "metrics": metrics,
                "report_error": report.get("error"),
                "email_status": email_status,
                "message": (
                    f"Reviewed {len(results)} issue(s). "
                    f"Findings: {metrics.get('total_findings', 0)}. "
                    f"Worst readiness: {metrics.get('overall_readiness', 'unknown')}."
                ),
            }
        )

    return output
