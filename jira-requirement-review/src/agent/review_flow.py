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


PREVIOUS_REVIEW_LABEL = "Previous review's open questions"


# Card types investigated as bugs rather than reviewed as requirements.
BUG_TYPES = frozenset({"bug", "defect"})

# What the code search reads the report from: what the person wrote, never the
# bot's own earlier replies or the previous review.
_REPORT_KINDS = ("trigger_comment", "requirement", "target_comments", "attachment")
_REPORT_CHARS = 20_000


def report_text(documents: list[dict[str, Any]]) -> str:
    """The bug report's own words, for picking code search terms."""
    parts = [str(d.get("text") or "") for d in documents if d.get("kind") in _REPORT_KINDS]
    return "\n".join(parts)[:_REPORT_CHARS]


def previous_review_document(questions: list[str]) -> dict[str, Any]:
    """The last review's questions, for this review to check against the comments.

    The person answers in the ticket's comments and asks again (2026-10-08). A
    question a later comment answers is settled: it must not be asked again.
    Only what is still open — or newly found — is reported.
    """
    lines = "\n".join(f"- {q}" for q in questions)
    return {
        "source_id": "previous_review",
        "source_label": PREVIOUS_REVIEW_LABEL,
        "kind": "review_history",
        "text": (
            "These questions were asked by the previous review of this ticket. Read the "
            "ticket's comments for answers. Treat a question a comment answers as settled and "
            "do not raise it again; report a question only if it is still unanswered, and use "
            "the answers as part of the requirement.\n" + lines
        ),
        "metadata": {"count": len(questions)},
    }


def resolve_document_keys(payload: dict[str, Any]) -> list[str]:
    """The documents to review when there is no ticket (2026-10-08).

    The orchestrator names them (``document_file_keys``: Task Creation's PDF
    and the files the person attached). On this agent's own form, files
    uploaded with no Issue Key and no query are the documents themselves.
    """
    named = payload.get("document_file_keys")
    if named:
        items = named if isinstance(named, list) else str(named).split(",")
        return list(dict.fromkeys(str(i).strip() for i in items if str(i or "").strip()))
    if (
        str(payload.get("issue_key") or "").strip()
        or str(payload.get("jql_override") or "").strip()
    ):
        return []
    return resolve_transcript_file_keys(payload)


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
    document_keys: list[str] | None = None,
    previous_questions: list[str] | None = None,
    review_kind: str = "requirement",
) -> dict[str, Any]:
    """Run the full context-gather -> analyze -> render -> (optional post) pipeline
    for one issue and return a single structured result dict.

    With ``document_keys`` there is no ticket: those files are what is reviewed,
    under the label ``issue_key``, and nothing can be posted (2026-10-08).
    """
    if document_keys:
        post_to_jira = False
        ctx = await execute(
            "gather_documents",
            document_keys,
            issue_key,
            team_id,
            start_to_close_timeout=timedelta(minutes=2),
        )
    else:
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

    documents = list(ctx.get("documents", []))
    if previous_questions:
        documents.append(previous_review_document(previous_questions))

    # A bug is investigated against the code (2026-10-22): asked for by the
    # router (review_kind="bug"), or because the card itself is a Bug.
    target_type = str((ctx.get("target_bundle") or {}).get("issue_type") or "")
    is_bug = review_kind == "bug" or target_type.strip().lower() in BUG_TYPES
    code: dict[str, Any] = {}
    if is_bug and not document_keys:
        code = await execute(
            "search_code",
            issue_key,
            ctx.get("issue_summary") or "",
            report_text(documents),
            start_to_close_timeout=timedelta(seconds=90),
        )
        documents.extend(code.get("documents") or [])
    analysis = await execute(
        "analyze_requirement",
        documents,
        issue_key,
        ctx.get("issue_summary"),
        model_id,
        bool((ctx.get("target_bundle") or {}).get("is_subtask")),
        "bug" if is_bug else "requirement",
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
    warnings = list(ctx.get("warnings", []))
    investigation = dict(analysis.get("investigation") or {})
    if is_bug:
        note = str(code.get("note") or "").strip()
        searched = str(code.get("searched") or "").strip()
        if note:
            investigation["code_note"] = note
        elif searched:
            investigation["code_note"] = f"Code searched for: {searched}."
        dropped = investigation.pop("files_dropped", 0)
        if dropped:
            warnings.append(
                f"{dropped} file(s) the model named were not among the files read, "
                "so they were left out."
            )

    result: dict[str, Any] = {
        "issue_key": issue_key,
        "summary": ctx.get("issue_summary"),
        "finding_count": finding_count,
        "by_category": by_category,
        "findings": findings,
        "readiness": readiness,
        "review_kind": "bug" if is_bug else "requirement",
        "investigation": investigation,
        # Open questions the ticket already lists — not repeated as findings.
        "known_questions": analysis.get("known_questions") or [],
        "warnings": warnings,
        # Carried so the delegated path can account for what was read and what
        # was not. Unused by the direct path, which reports through the draft.
        "documents": [*(ctx.get("documents") or []), *(code.get("documents") or [])],
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
    document_keys = resolve_document_keys(payload)

    if not issue_key and not jql_override and not document_keys:
        return [
            {
                "status": "error",
                "error": "issue_key_or_jql_required",
                "message": (
                    "Provide 'issue_key' (e.g. ABC-123), a Sprint/JQL query "
                    "('jql_override', e.g. 'sprint = \"Sprint 42\"'), or upload the "
                    "documents to review."
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
        previous_questions=[
            str(q) for q in payload.get("previous_questions") or [] if str(q).strip()
        ],
        review_kind=str(payload.get("review_kind") or "requirement").strip().lower(),
    )
    generate_docx = flag(payload, "generate_docx")
    # TEMPORARILY DISABLED: email delivery is not exposed as a trigger (see
    # metadata.json) and payload["send_email"] is ignored regardless of what's
    # sent — Pulsar template/recipient config isn't verified for this agent yet.
    # Re-enable by restoring `bool(payload.get("send_email", False))` once confirmed.
    send_email = False

    if document_keys and not issue_key and not jql_override:
        # No ticket: review the documents themselves (2026-10-08).
        label = str(payload.get("document_title") or "").strip() or "Document review"
        logger.info("Requirement review starting (documents): %d file(s)", len(document_keys))
        results = [await _review_one_issue(label, execute, **common, document_keys=document_keys)]
    elif jql_override:
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
        elif generate_docx and report.get("local_path"):
            # A laptop run: the report was saved on disk, not uploaded.
            output.append(
                {
                    "type": "local_file",
                    "title": f"Requirement Review Report — {label}",
                    "path": report["local_path"],
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
