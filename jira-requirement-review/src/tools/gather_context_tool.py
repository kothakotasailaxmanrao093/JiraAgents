"""@tool: gather all review context (Jira issue + parent + subtasks + transcript).

Thin activity shim — constructs the Jira client from env, runs the registered
``ContextSource``s, and returns JSON-safe documents. All Jira/storage I/O happens
here (inside the activity), never in the workflow.
"""

from __future__ import annotations

import logging
from typing import Any

from aetherion_sdk import tool
from dotenv import load_dotenv

from config import resolve_team_id
from context.base import FetchContext
from context.budget import trim_documents_to_budget
from context.registry import build_sources
from jira.client import jira_client_from_env
from shared.confluence_text import find_unread_links

load_dotenv()
logger = logging.getLogger(__name__)


# Document kinds that can carry the requirement itself. An attachment or a
# linked Confluence page routinely IS the specification — a ticket whose
# description reads "see attached" used to abort as "nothing to review", which
# is the opposite of the truth.
REQUIREMENT_BEARING_KINDS = frozenset({"requirement", "attachment", "confluence"})


def requirement_is_present(target: dict[str, Any], documents: list[dict[str, Any]]) -> bool:
    """True when something reviewable was found, wherever it lives."""
    if (target.get("description_text") or "").strip():
        return True
    return any(
        d.get("kind") in REQUIREMENT_BEARING_KINDS and (d.get("text") or "").strip()
        for d in documents
    )


@tool(name="gather_context")
async def gather_context(
    issue_key: str,
    include_parent: bool = True,
    include_subtasks: bool = True,
    include_linked_issues: bool = True,
    include_attachments: bool = True,
    include_confluence: bool = True,
    trigger_comment_id: str | None = None,
    transcript_file_keys: list[str] | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    logger.info(
        "gather_context: issue=%s parent=%s subtasks=%s linked_issues=%s "
        "attachments=%s confluence=%s transcript=%s",
        issue_key,
        include_parent,
        include_subtasks,
        include_linked_issues,
        include_attachments,
        include_confluence,
        len(transcript_file_keys or []),
    )

    ctx = FetchContext(
        issue_key=issue_key,
        include_parent=include_parent,
        include_subtasks=include_subtasks,
        include_linked_issues=include_linked_issues,
        include_attachments=include_attachments,
        include_confluence=include_confluence,
        trigger_comment_id=trigger_comment_id,
        transcript_file_keys=list(transcript_file_keys or []),
        team_id=resolve_team_id(team_id),
    )

    try:
        async with jira_client_from_env() as jira:
            me = await jira.preflight()
            ctx.jira = jira
            try:
                ctx.target_bundle = await jira.get_issue_bundle(issue_key)
            except Exception as e:
                logger.error("Target issue %s fetch failed: %s", issue_key, e, exc_info=True)
                acct = (
                    f"{me.get('emailAddress') or me.get('displayName')}"
                    if isinstance(me, dict)
                    else "unknown (auth/preflight failed)"
                )
                detail = f"Could not fetch issue {issue_key}: {e}"
                # Jira returns 404 for BOTH "missing" and "no permission" — make the
                # likely cause (permission/credentials) explicit and name the account.
                if "404" in str(e):
                    detail += (
                        f". Jira authenticated as '{acct}'. A 404 here usually means that "
                        "account cannot see the issue (no Browse-project permission) rather "
                        "than the issue being missing — verify the JIRA_EMAIL/JIRA_API_TOKEN "
                        "account can open the issue in a browser, and that the token is current."
                    )
                return {
                    "documents": [],
                    "target_bundle": {},
                    "has_requirement": False,
                    "issue_summary": None,
                    "counts": {},
                    "warnings": [],
                    "error": detail,
                }

            # Surface silently-capped subtasks so "ALL subtasks" coverage is honest.
            subtask_total = ctx.target_bundle.get("subtask_total", 0)
            reviewed_subtasks = len(ctx.target_bundle.get("subtask_keys") or [])
            if include_subtasks and subtask_total > reviewed_subtasks:
                w = (
                    f"Only the first {reviewed_subtasks} of {subtask_total} subtasks were "
                    "reviewed (payload cap)."
                )
                ctx.warnings.append(w)
                logger.warning(w)

            # Same honesty check for linked issues.
            linked_total = ctx.target_bundle.get("linked_issues_total", 0)
            reviewed_linked = len(ctx.target_bundle.get("linked_issues") or [])
            if include_linked_issues and linked_total > reviewed_linked:
                w = (
                    f"Only the first {reviewed_linked} of {linked_total} linked issues were "
                    "reviewed (payload cap)."
                )
                ctx.warnings.append(w)
                logger.warning(w)

            documents: list[dict[str, Any]] = []
            for source in build_sources(ctx):
                if not source.is_enabled(ctx):
                    continue
                try:
                    for doc in await source.load(ctx):
                        documents.append(doc.to_dict())
                except Exception as e:
                    ctx.warnings.append(f"{source.source_id} failed: {e}")
                    logger.warning("Source %s failed: %s", source.source_id, e, exc_info=True)

            # Aggregate budget: bound the combined payload that crosses the
            # workflow<->activity boundary (per-field caps alone do not).
            documents, trim_warning = trim_documents_to_budget(documents)
            if trim_warning:
                ctx.warnings.append(trim_warning)
                logger.warning(trim_warning)

            # Bug 6: a link that is not a Confluence page on this site (a
            # Google Doc, a different Atlassian site, a Notion page) is
            # invisible to find_references — correctly, since that function
            # only promises Confluence pages. But nothing else said the link
            # was ever there, so "the spec is at <link>" read as a complete
            # requirement once the URL characters were gone. Named here, not
            # fetched — this account may have no way to reach it at all.
            bundle = ctx.target_bundle or {}
            texts = [bundle.get("description_text") or ""]
            texts += [c.get("body") or "" for c in bundle.get("comments") or []]
            unread = find_unread_links(texts, jira.base)
            for url in unread:
                w = (
                    "Link in the ticket could not be read (not a Confluence "
                    f"page on this site): {url}"
                )
                ctx.warnings.append(w)
                logger.info(w)
    except ValueError as e:  # missing Jira env config — fatal for this run
        logger.error("gather_context configuration error: %s", e)
        return {
            "documents": [],
            "target_bundle": {},
            "has_requirement": False,
            "issue_summary": None,
            "counts": {},
            "warnings": [],
            "error": str(e),
        }

    target = ctx.target_bundle or {}
    has_requirement = requirement_is_present(target, documents)
    counts = {
        "parent": 1 if target.get("parent_key") else 0,
        "subtasks": len(target.get("subtask_keys") or []),
        "subtasks_total": target.get("subtask_total", len(target.get("subtask_keys") or [])),
        "linked_issues": len(target.get("linked_issues") or []),
        "linked_issues_total": target.get(
            "linked_issues_total", len(target.get("linked_issues") or [])
        ),
        "attachments": len([d for d in documents if d.get("kind") == "attachment"]),
        "attachments_total": target.get("attachments_total", 0),
        "confluence_pages": len([d for d in documents if d.get("kind") == "confluence"]),
        "documents": len(documents),
    }
    logger.info(
        "gather_context done: %d document(s), %d warning(s)",
        len(documents),
        len(ctx.warnings),
    )
    return {
        "documents": documents,
        # Carried so the delegated path can say what the ticket itself lacks
        # (no description, no acceptance criteria) rather than only what failed.
        "target_bundle": target,
        "has_requirement": has_requirement,
        "issue_summary": target.get("summary"),
        "counts": counts,
        "warnings": ctx.warnings,
        "error": None,
    }
