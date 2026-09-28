"""Jira Cloud REST v3 integration for the work-breakdown agent.

Follows the conventions already established in the sibling ``asurint`` project:
credentials from the environment, one pooled ``httpx.AsyncClient`` per call,
secrets only ever logged through ``_mask``, JQL string values escaped, and
failures returned as data rather than raised across the activity boundary.

Two behaviours are specific to this agent:

* **Preflight.** The Jira project and the issue types the breakdown needs are
  verified *before* the first write, so a missing Sub-task type fails with
  nothing created rather than half a hierarchy.
* **Idempotency.** Every hierarchy carries a deterministic label derived from
  the requirement text and target project. A retry finds that label and returns
  the existing issues instead of creating duplicates.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

import httpx
from common_lib.utils.logger import setup_logger

from src.jira.adf import (
    epic_description,
    story_description,
    subtask_description,
)
from src.jira.client import (
    IDEMPOTENCY_LABEL_PREFIX,
    _http_error_text,
    _quote,
    browse_url,
    issue_type_names,
)
from src.jira.context import fetch_epic_children
from src.models.schemas import (
    JiraIssueRef,
    JiraResult,
    ResultStatus,
    WorkBreakdown,
)

logger = setup_logger(__name__)


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def idempotency_key(requirement: str, project_key: str, provided: str = "") -> str:
    """Deterministic key for one (requirement, project) pair.

    A caller-supplied correlation id wins when present; otherwise the key is a
    hash of the normalised requirement text, so an identical retry — from a
    Temporal replay, a double-click, or a re-run of the same trigger — resolves
    to the same label and finds the issues already created.
    """
    if provided.strip():
        digest_source = provided.strip()
    else:
        normalised = re.sub(r"\s+", " ", requirement.strip().lower())
        digest_source = f"{project_key.upper()}::{normalised}"
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:16]
    return f"{IDEMPOTENCY_LABEL_PREFIX}-{digest}"


async def find_existing(
    client: httpx.AsyncClient, project_key: str, key_label: str
) -> list[dict[str, Any]]:
    """Return issues already carrying this run's idempotency label."""
    jql = f"project = {_quote(project_key)} AND labels = {_quote(key_label)}"
    resp = await client.get(
        "/rest/api/3/search/jql",
        params={"jql": jql, "maxResults": 100, "fields": "summary,issuetype,parent"},
    )
    resp.raise_for_status()
    return resp.json().get("issues", [])


# --------------------------------------------------------------------------
# Issue creation
# --------------------------------------------------------------------------


async def create_issue(
    client: httpx.AsyncClient,
    base_url: str,
    project_key: str,
    issue_type: str,
    summary: str,
    description: dict[str, Any],
    labels: list[str],
    parent_key: str = "",
) -> JiraIssueRef:
    """Create one issue and return its real key/id/url. Raises on failure."""
    fields: dict[str, Any] = {
        "project": {"key": project_key},
        "issuetype": {"name": issue_type},
        "summary": summary[:255],
        "description": description,
        "labels": labels,
    }
    if parent_key:
        fields["parent"] = {"key": parent_key}

    resp = await client.post("/rest/api/3/issue", json={"fields": fields})
    resp.raise_for_status()
    data = resp.json()
    key = data.get("key", "")
    logger.info(f"Created Jira {issue_type} {key} (parent={parent_key or '-'})")
    return JiraIssueRef(
        key=key,
        id=str(data.get("id", "")),
        url=browse_url(base_url, key),
        issue_type=issue_type,
        summary=summary[:255],
        parent_key=parent_key,
    )


async def link_related(client: httpx.AsyncClient, source_key: str, created_key: str) -> None:
    """Link new work back to the ticket it was asked for on ("relates to").

    Without it BGV-16 was created from a comment on BGV-15 and nothing on
    BGV-15 said where the work went (2026-09-24).
    """
    resp = await client.post(
        "/rest/api/3/issueLink",
        json={
            "type": {"name": "Relates"},
            "inwardIssue": {"key": source_key},
            "outwardIssue": {"key": created_key},
        },
    )
    resp.raise_for_status()


async def link_story_to_epic(client: httpx.AsyncClient, story_key: str, epic_key: str) -> str:
    """Link a Story to its Epic using whichever mechanism this Jira supports.

    Modern Jira Cloud (team-managed and company-managed) accepts ``parent`` on
    the Story. Older company-managed projects still require the classic "Epic
    Link" custom field. The field id is discovered at runtime rather than
    assumed, and can be pinned with ``JIRA_EPIC_LINK_FIELD_ID``.

    Returns the mechanism used, for the caller to log.
    """
    try:
        resp = await client.put(
            f"/rest/api/3/issue/{story_key}",
            json={"fields": {"parent": {"key": epic_key}}},
        )
        resp.raise_for_status()
        return "parent"
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (400, 404):
            raise
        logger.info(
            f"'parent' link rejected for {story_key} -> {epic_key} "
            f"({_http_error_text(exc)}); trying the Epic Link field."
        )

    field_id = await resolve_epic_link_field(client)
    if not field_id:
        raise RuntimeError(
            "Jira rejected the 'parent' field and no 'Epic Link' custom field "
            "exists in this instance; cannot link the story to its epic."
        )
    resp = await client.put(f"/rest/api/3/issue/{story_key}", json={"fields": {field_id: epic_key}})
    resp.raise_for_status()
    return f"epic-link:{field_id}"


async def resolve_epic_link_field(client: httpx.AsyncClient) -> str:
    """Find the classic 'Epic Link' custom field id, or "" when absent."""
    override = os.environ.get("JIRA_EPIC_LINK_FIELD_ID", "").strip()
    if override:
        return override
    resp = await client.get("/rest/api/3/field")
    resp.raise_for_status()
    for field in resp.json():
        if (field.get("name") or "").strip().lower() == "epic link":
            return str(field.get("id", ""))
    return ""


# --------------------------------------------------------------------------
# Hierarchy orchestration
# --------------------------------------------------------------------------


def _failure(
    key_label: str,
    created: list[JiraIssueRef],
    failed_issue: str,
    reason: str,
    retry_safe: bool,
    epic: JiraIssueRef | None,
    stories: list[JiraIssueRef],
    subtasks: list[JiraIssueRef],
) -> JiraResult:
    return JiraResult(
        status=ResultStatus.JIRA_CREATION_FAILED,
        epic=epic,
        stories=stories,
        subtasks=subtasks,
        created_keys=[ref.key for ref in created],
        failed_issue=failed_issue,
        failure_reason=reason,
        retry_safe=retry_safe,
        idempotency_key=key_label,
        summary=(
            f"{len(created)} issue(s) created before the failure. " f"Failed on: {failed_issue}."
        ),
    )


def _key_number(ref: JiraIssueRef) -> int:
    """Sort key for an issue key like ``TT-42`` → 42, so ordering is numeric."""
    tail = ref.key.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _reuse(base_url: str, key_label: str, issues: list[dict[str, Any]]) -> JiraResult:
    """Rebuild a result from issues already tagged with this idempotency key."""
    epic: JiraIssueRef | None = None
    stories: list[JiraIssueRef] = []
    subtasks: list[JiraIssueRef] = []
    types = issue_type_names()

    for issue in issues:
        fields = issue.get("fields", {})
        type_name = (fields.get("issuetype") or {}).get("name", "")
        ref = JiraIssueRef(
            key=issue.get("key", ""),
            id=str(issue.get("id", "")),
            url=browse_url(base_url, issue.get("key", "")),
            issue_type=type_name,
            summary=fields.get("summary", "") or "",
            parent_key=(fields.get("parent") or {}).get("key", "") or "",
        )
        lowered = type_name.strip().lower()
        if lowered == types["epic"].lower():
            epic = ref
        elif (fields.get("issuetype") or {}).get("subtask") or lowered in {
            "sub-task",
            "subtask",
            types["subtask"].lower(),
        }:
            subtasks.append(ref)
        else:
            stories.append(ref)

    # Jira returns these in its own order; sort by issue number so the caller
    # sees the same ascending order the create path produces.
    stories.sort(key=_key_number)
    subtasks.sort(key=_key_number)

    return JiraResult(
        status=ResultStatus.JIRA_CREATED,
        epic=epic,
        stories=stories,
        subtasks=subtasks,
        # This run created nothing. created_keys must stay empty or it reports
        # work that did not happen; the keys live in reused_keys instead.
        created_keys=[],
        reused_keys=([epic.key] if epic else []) + [r.key for r in stories + subtasks],
        idempotency_key=key_label,
        reused_existing=True,
        # An epic that was already there was, by definition, not created now.
        epic_reused=epic is not None,
        summary=(
            f"Existing issues for this requirement were found and reused: "
            f"{len(stories)} Story/Stories and {len(subtasks)} Subtask(s)"
            f"{' under 1 Epic' if epic else ''}. Nothing was created again."
        ),
    )


def _normalised_title(text: str) -> str:
    """A title reduced to what makes it the same piece of work.

    Case, punctuation and runs of whitespace are ignored, so a sub-task is
    recognised as already present even if Jira or a person tidied its wording.
    """
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


async def create_hierarchy(
    client: httpx.AsyncClient,
    base_url: str,
    project_key: str,
    breakdown: WorkBreakdown,
    key_label: str,
    extra_labels: list[str] | None = None,
    existing_epic_key: str = "",
    attach_to_key: str = "",
    types: dict[str, str] | None = None,
) -> JiraResult:
    """Create the Epic/Story/Subtask hierarchy for a validated breakdown.

    Ordering is deliberate: Epic first (so stories can reference a real key),
    then each Story, then that Story's Subtasks. On any failure the work stops
    and everything created so far is reported, together with whether a retry is
    safe — it is, because the idempotency label makes a re-run resolve to the
    same issues.
    """
    # Resolved against the project by the caller; the configured names otherwise.
    types = types or issue_type_names()
    labels = [key_label, *(extra_labels or [])]
    created: list[JiraIssueRef] = []
    epic_ref: JiraIssueRef | None = None
    story_refs: list[JiraIssueRef] = []
    subtask_refs: list[JiraIssueRef] = []

    # Someone pointed at a ticket and asked for it to be broken down, and the
    # work turned out to be one capability. That ticket already *is* the work
    # item — creating a Story beside it produces a copy that says the same
    # thing, and hangs the sub-tasks off the copy instead of the ticket that
    # was asked about. Attach directly instead.
    if attach_to_key and len(breakdown.stories) == 1 and breakdown.epic is None:
        story = breakdown.stories[0]
        parent_ref = JiraIssueRef(
            key=attach_to_key,
            url=browse_url(base_url, attach_to_key),
            summary=story.title,
        )
        logger.info(f"Attaching sub-tasks directly to {attach_to_key} (no duplicate Story)")

        # What is already on this ticket. The idempotency label cannot help
        # here: it is keyed per comment, so asking the same thing in a second
        # comment produces a different label and a second identical set. Nor
        # can duplicate detection — it drops Sub-tasks from comparison, because
        # their fixed "Define the rules for:" prefixes drag every score down.
        # Three runs of one request left FL-53 carrying nine Sub-tasks, three
        # of each. Compare against the parent's own children instead.
        try:
            siblings = await fetch_epic_children(client, attach_to_key, project_key)
        except httpx.HTTPError as exc:
            # Without this list a repeat would duplicate silently, so stop
            # rather than guess that the ticket is empty.
            logger.error(f"Could not read existing sub-tasks of {attach_to_key}: {exc}")
            return _failure(
                key_label,
                created,
                f"Existing sub-tasks of {attach_to_key}",
                str(exc),
                True,
                None,
                [],
                subtask_refs,
            )
        already = {_normalised_title(c.summary) for c in siblings}

        skipped: list[JiraIssueRef] = []
        for subtask in story.subtasks:
            if _normalised_title(subtask.title) in already:
                existing = next(
                    c
                    for c in siblings
                    if _normalised_title(c.summary) == _normalised_title(subtask.title)
                )
                skipped.append(
                    JiraIssueRef(
                        key=existing.key,
                        url=browse_url(base_url, existing.key),
                        issue_type=existing.issue_type,
                        summary=existing.summary,
                        parent_key=attach_to_key,
                    )
                )
                continue
            try:
                ref = await create_issue(
                    client,
                    base_url,
                    project_key,
                    types["subtask"],
                    subtask.title,
                    subtask_description(subtask),
                    labels,
                    parent_key=attach_to_key,
                )
            except httpx.HTTPError as exc:
                reason = (
                    _http_error_text(exc) if isinstance(exc, httpx.HTTPStatusError) else str(exc)
                )
                logger.error(f"Sub-task creation failed on {attach_to_key}: {reason}")
                return _failure(
                    key_label,
                    created,
                    f"Sub-task '{subtask.title}'",
                    reason,
                    True,
                    None,
                    [],
                    subtask_refs,
                )
            created.append(ref)
            subtask_refs.append(ref)

        if skipped and not subtask_refs:
            summary = (
                f"Nothing was added. {attach_to_key} already carries all "
                f"{len(skipped)} of these sub-tasks. Delete them first if you "
                f"want the work broken down again."
            )
        elif skipped:
            summary = (
                f"{len(subtask_refs)} Subtask(s) added to {attach_to_key}; "
                f"{len(skipped)} were already there and were left alone. "
                f"No new Story was created — {attach_to_key} already describes this work."
            )
        else:
            summary = (
                f"{len(subtask_refs)} Subtask(s) added to {attach_to_key}. "
                f"No new Story was created — {attach_to_key} already describes this work."
            )

        logger.info(
            f"Attached to {attach_to_key} in {project_key}: "
            f"created={[r.key for r in created] or '-'} "
            f"skipped_as_duplicate={[r.key for r in skipped] or '-'} label={key_label}"
        )
        return JiraResult(
            status=ResultStatus.JIRA_CREATED,
            epic=None,
            stories=[parent_ref],
            subtasks=subtask_refs + skipped,
            created_keys=[r.key for r in created],
            reused_keys=[r.key for r in skipped],
            reused_existing=bool(skipped and not subtask_refs),
            idempotency_key=key_label,
            attached_to=attach_to_key,
            summary=summary,
        )

    epic_reused = False
    if existing_epic_key:
        # inspect_jira_context already proved this epic exists, is an Epic, and
        # belongs to this project. Reuse it instead of creating a second one.
        epic_ref = JiraIssueRef(
            key=existing_epic_key,
            url=browse_url(base_url, existing_epic_key),
            issue_type=types["epic"],
        )
        epic_reused = True
        logger.info(f"Attaching new stories to existing epic {existing_epic_key}")
    elif breakdown.epic is not None:
        story_titles = breakdown.epic.related_user_stories(breakdown.stories)
        try:
            epic_ref = await create_issue(
                client,
                base_url,
                project_key,
                types["epic"],
                breakdown.epic.jira_summary,
                epic_description(breakdown.epic, story_titles),
                labels,
            )
            created.append(epic_ref)
        except (httpx.HTTPError, RuntimeError) as exc:
            reason = _http_error_text(exc) if isinstance(exc, httpx.HTTPStatusError) else str(exc)
            logger.error(f"Epic creation failed: {reason}")
            return _failure(
                key_label,
                created,
                f"Epic '{breakdown.epic.jira_summary}'",
                reason,
                True,
                None,
                [],
                [],
            )

    for story in breakdown.stories:
        try:
            story_ref = await create_issue(
                client,
                base_url,
                project_key,
                types["story"],
                story.title,
                story_description(story),
                labels,
                parent_key=epic_ref.key if epic_ref else "",
            )
        except httpx.HTTPStatusError as exc:
            # A rejected inline parent usually means this project wants the
            # classic Epic Link field: create unparented, then link.
            if epic_ref is None or exc.response.status_code not in (400, 404):
                reason = _http_error_text(exc)
                logger.error(f"Story creation failed: {reason}")
                return _failure(
                    key_label,
                    created,
                    f"Story '{story.title}'",
                    reason,
                    True,
                    epic_ref,
                    story_refs,
                    subtask_refs,
                )
            try:
                story_ref = await create_issue(
                    client,
                    base_url,
                    project_key,
                    types["story"],
                    story.title,
                    story_description(story),
                    labels,
                )
                mechanism = await link_story_to_epic(client, story_ref.key, epic_ref.key)
                story_ref = story_ref.model_copy(update={"parent_key": epic_ref.key})
                logger.info(f"Linked {story_ref.key} to {epic_ref.key} via {mechanism}")
            except (httpx.HTTPError, RuntimeError) as link_exc:
                reason = (
                    _http_error_text(link_exc)
                    if isinstance(link_exc, httpx.HTTPStatusError)
                    else str(link_exc)
                )
                logger.error(f"Story/epic linking failed: {reason}")
                return _failure(
                    key_label,
                    created,
                    f"Story '{story.title}'",
                    reason,
                    True,
                    epic_ref,
                    story_refs,
                    subtask_refs,
                )
        except httpx.HTTPError as exc:
            logger.error(f"Story creation failed: {exc}")
            return _failure(
                key_label,
                created,
                f"Story '{story.title}'",
                str(exc),
                True,
                epic_ref,
                story_refs,
                subtask_refs,
            )

        story_refs.append(story_ref)
        created.append(story_ref)

        for subtask in story.subtasks:
            try:
                subtask_ref = await create_issue(
                    client,
                    base_url,
                    project_key,
                    types["subtask"],
                    subtask.title,
                    subtask_description(subtask),
                    labels,
                    parent_key=story_ref.key,
                )
            except httpx.HTTPError as exc:
                reason = (
                    _http_error_text(exc) if isinstance(exc, httpx.HTTPStatusError) else str(exc)
                )
                logger.error(f"Subtask creation failed: {reason}")
                return _failure(
                    key_label,
                    created,
                    f"Subtask '{subtask.title}'",
                    reason,
                    True,
                    epic_ref,
                    story_refs,
                    subtask_refs,
                )
            subtask_refs.append(subtask_ref)
            created.append(subtask_ref)

    counts = breakdown.issue_count()
    if epic_reused:
        # An epic that already existed was not created by this run.
        counts = {**counts, "epics": 0}
    parts = []
    if counts["epics"]:
        parts.append(f"{counts['epics']} Epic")
    parts.append(f"{counts['stories']} Stor{'y' if counts['stories'] == 1 else 'ies'}")
    parts.append(f"{counts['subtasks']} Subtask{'' if counts['subtasks'] == 1 else 's'}")

    summary = (
        ", ".join(parts[:-1]) + f", and {parts[-1]} created successfully."
        if len(parts) > 1
        else f"{parts[0]} created successfully."
    )
    if epic_reused:
        summary += f" Attached to existing epic {epic_ref.key}."

    story_refs.sort(key=_key_number)
    subtask_refs.sort(key=_key_number)

    # Every failure below logs; success did not, so a completed run left no
    # record of what it made. Tracing "where did FL-120 come from?" meant
    # reading Jira instead of the log.
    logger.info(
        f"Created in {project_key}: {len(created)} issue(s) "
        f"[epic={epic_ref.key if epic_ref else '-'}"
        f"{' (reused)' if epic_reused else ''} | "
        f"stories={[r.key for r in story_refs] or '-'} | "
        f"subtasks={[r.key for r in subtask_refs] or '-'}] label={key_label}"
    )

    return JiraResult(
        status=ResultStatus.JIRA_CREATED,
        epic=epic_ref,
        stories=story_refs,
        subtasks=subtask_refs,
        created_keys=[ref.key for ref in created],
        idempotency_key=key_label,
        epic_reused=epic_reused,
        summary=summary,
    )
