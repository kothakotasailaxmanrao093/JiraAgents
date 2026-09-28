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

from typing import Any

import httpx
from common_lib.utils.logger import setup_logger

from src.jira.adf import _plain_text, adf
from src.jira.keywords import (  # noqa: F401
    AGENT_FOOTER,
    AGENT_SIGNATURE,
    DEFAULT_AWAITING_LABEL,
    DEFAULT_PROCESSED_LABEL,
    DEFAULT_TRIGGER_KEYWORD,
    PROCESSED_COMMENTS_PROPERTY,
    awaiting_label,
    is_agent_comment,
    mentions_trigger,
    processed_label,
    trigger_keyword,
)
from src.models.schemas import (
    CommentInfo,
    DuplicateMatch,
    HistoryEntry,
    JiraIssueRef,
    LinkedIssue,
    WorklogEntry,
)
from src.shared.adf import comment_text

logger = setup_logger(__name__)


# --------------------------------------------------------------------------
# Webhook ingestion: reading the issue that triggered the run
# --------------------------------------------------------------------------

# Fields the pipeline actually reads. Requesting a fixed list (rather than
# "*all") keeps the payload small on issues with dozens of custom fields.
_ISSUE_FIELDS = (
    "summary,description,issuetype,status,reporter,created,labels,project,"
    "attachment,issuelinks,parent,subtasks,comment,worklog"
)


def _person(node: Any) -> str:
    """Display name from a Jira user object."""
    if not isinstance(node, dict):
        return ""
    return str(node.get("displayName") or node.get("name") or node.get("emailAddress") or "")


def sign(blocks: list[tuple[str, Any]], headline: str) -> dict[str, Any]:
    """Render an agent reply: a signed heading, the blocks, a signed footer."""
    return adf([(f"{AGENT_SIGNATURE} · {headline}", "")] + blocks + [("", AGENT_FOOTER)])


async def answered_comment_ids(client: httpx.AsyncClient, issue_key: str) -> set[str]:
    """Comment ids this agent has already answered on an issue.

    Kept in a Jira issue property rather than in memory so the same ticket can
    take many requests over its life, and so a restart of the service does not
    make it answer everything again.
    """
    try:
        resp = await client.get(
            f"/rest/api/3/issue/{issue_key}/properties/{PROCESSED_COMMENTS_PROPERTY}"
        )
        if resp.status_code == 404:
            return set()
        resp.raise_for_status()
        value = resp.json().get("value") or {}
        return {str(x) for x in (value.get("ids") or [])}
    except httpx.HTTPError as exc:
        # Better to risk answering twice than to refuse every request.
        logger.warning(f"Could not read answered comments for {issue_key}: {exc}")
        return set()


async def mark_comment_answered(client: httpx.AsyncClient, issue_key: str, comment_id: str) -> None:
    """Record that a comment has been answered. Never raises."""
    if not comment_id:
        return
    try:
        existing = await answered_comment_ids(client, issue_key)
        existing.add(str(comment_id))
        # Keep the list from growing without bound on a long-lived ticket.
        ids = sorted(existing, key=lambda v: (len(v), v))[-200:]
        resp = await client.put(
            f"/rest/api/3/issue/{issue_key}/properties/{PROCESSED_COMMENTS_PROPERTY}",
            json={"ids": ids},
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(f"Could not record answered comment {comment_id} on {issue_key}: {exc}")


async def fetch_issue(client: httpx.AsyncClient, issue_key: str) -> dict[str, Any]:
    """Read one issue with everything the pipeline needs, in a single call.

    ``expand=changelog`` brings the field history back on the same request
    instead of a second round trip.
    """
    resp = await client.get(
        f"/rest/api/3/issue/{issue_key.strip().upper()}",
        params={"fields": _ISSUE_FIELDS, "expand": "changelog"},
    )
    resp.raise_for_status()
    return resp.json()


def parse_comments(fields: dict[str, Any], limit: int = 50) -> list[CommentInfo]:
    """Flatten the comment list out of ADF, oldest first."""
    container = fields.get("comment") or {}
    raw = container.get("comments") or []
    out: list[CommentInfo] = []
    for item in raw[-limit:]:
        # Without the "@<author>" Jira pre-fills on a Reply — see comment_text.
        body = comment_text(item.get("body"), keep=trigger_keyword())
        if not body:
            continue
        out.append(
            CommentInfo(
                id=str(item.get("id", "")),
                author=_person(item.get("author")),
                created=str(item.get("created", ""))[:19],
                body=body,
            )
        )
    return out


def parse_history(payload: dict[str, Any], limit: int = 50) -> list[HistoryEntry]:
    """Flatten the changelog into one entry per changed field."""
    histories = (payload.get("changelog") or {}).get("histories") or []
    out: list[HistoryEntry] = []
    for entry in histories[-limit:]:
        author = _person(entry.get("author"))
        created = str(entry.get("created", ""))[:19]
        for item in entry.get("items") or []:
            out.append(
                HistoryEntry(
                    created=created,
                    author=author,
                    field=str(item.get("field", "")),
                    from_value=str(item.get("fromString") or "")[:120],
                    to_value=str(item.get("toString") or "")[:120],
                )
            )
    return out


def parse_worklogs(fields: dict[str, Any], limit: int = 30) -> list[WorklogEntry]:
    """Flatten work-log entries."""
    container = fields.get("worklog") or {}
    out: list[WorklogEntry] = []
    for item in (container.get("worklogs") or [])[-limit:]:
        out.append(
            WorklogEntry(
                author=_person(item.get("author")),
                started=str(item.get("started", ""))[:19],
                time_spent=str(item.get("timeSpent", "")),
                comment=_plain_text(item.get("comment"))[:300],
            )
        )
    return out


def parse_links(fields: dict[str, Any]) -> list[LinkedIssue]:
    """Collect linked issues, the parent, and any sub-tasks, with relationships."""
    out: list[LinkedIssue] = []

    for link in fields.get("issuelinks") or []:
        link_type = link.get("type") or {}
        for direction, label in (("outwardIssue", "outward"), ("inwardIssue", "inward")):
            issue = link.get(direction)
            if not issue:
                continue
            relationship = str(
                link_type.get("outward" if direction == "outwardIssue" else "inward")
                or link_type.get("name")
                or label
            )
            inner = issue.get("fields") or {}
            out.append(
                LinkedIssue(
                    key=issue.get("key", ""),
                    summary=inner.get("summary", "") or "",
                    issue_type=(inner.get("issuetype") or {}).get("name", "") or "",
                    status=(inner.get("status") or {}).get("name", "") or "",
                    relationship=relationship,
                )
            )

    parent = fields.get("parent")
    if parent:
        inner = parent.get("fields") or {}
        out.append(
            LinkedIssue(
                key=parent.get("key", ""),
                summary=inner.get("summary", "") or "",
                issue_type=(inner.get("issuetype") or {}).get("name", "") or "",
                status=(inner.get("status") or {}).get("name", "") or "",
                relationship="parent of this issue",
            )
        )

    for child in fields.get("subtasks") or []:
        inner = child.get("fields") or {}
        out.append(
            LinkedIssue(
                key=child.get("key", ""),
                summary=inner.get("summary", "") or "",
                issue_type=(inner.get("issuetype") or {}).get("name", "") or "",
                status=(inner.get("status") or {}).get("name", "") or "",
                relationship="sub-task of this issue",
            )
        )

    return [link for link in out if link.key]


async def download_attachment(client: httpx.AsyncClient, url: str) -> bytes:
    """Fetch attachment bytes.

    The content URL is absolute and redirects to storage, so redirects are
    followed and the JSON ``Accept`` header from the shared client is dropped.
    """
    resp = await client.get(url, headers={"Accept": "*/*"}, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


async def add_comment(client: httpx.AsyncClient, issue_key: str, body: dict[str, Any]) -> str:
    """Post a comment on an issue and return its id."""
    resp = await client.post(f"/rest/api/3/issue/{issue_key}/comment", json={"body": body})
    resp.raise_for_status()
    return str(resp.json().get("id", ""))


async def set_labels(
    client: httpx.AsyncClient,
    issue_key: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> None:
    """Add and/or remove labels without touching the rest of the issue.

    Uses the ``update`` verb rather than ``fields`` so concurrent label edits by
    a human are not clobbered.
    """
    operations = [{"add": label} for label in (add or [])]
    operations += [{"remove": label} for label in (remove or [])]
    if not operations:
        return
    resp = await client.put(
        f"/rest/api/3/issue/{issue_key}", json={"update": {"labels": operations}}
    )
    resp.raise_for_status()


def outcome_comment(
    *,
    headline: str,
    situation: str = "",
    created: list[JiraIssueRef] | None = None,
    questions: list[str] | None = None,
    duplicates: list[DuplicateMatch] | None = None,
    errors: list[str] | None = None,
    context_notes: list[str] | None = None,
    conflicts: list[str] | None = None,
    notified: str = "",
    quality_warning: str = "",
) -> dict[str, Any]:
    """Build the ADF comment the agent posts back on the trigger issue.

    Whatever happened, the ticket itself carries the record — an email can be
    missed or filtered, the comment cannot.
    """
    blocks: list[tuple[str, Any]] = [(f"{AGENT_SIGNATURE} · {headline}", "")]
    # First, before anything else, when the wording was not produced by the
    # model. It went only into the run result before, where the person reading
    # the ticket never sees it — so they judge the generic wording as the
    # agent's normal quality rather than as an outage.
    if quality_warning:
        blocks.append(("⚠ Please review the wording", quality_warning))
    if situation:
        blocks.append(("What happened", situation))
    if created:
        blocks.append(
            ("Created in Jira", [f"{ref.issue_type} {ref.key} — {ref.summary}" for ref in created])
        )
    if questions:
        blocks.append(("Answers needed", list(questions)))
    if duplicates:
        blocks.append(
            (
                "Existing work this overlaps",
                [
                    f'"{m.proposed_title}" matches {m.existing_key} '
                    f'("{m.existing_summary}", similarity {m.score})'
                    for m in duplicates
                ],
            )
        )
    if errors:
        blocks.append(("Problems", [e for e in errors if e]))
    # Anything that was linked but could not be read. Without this the agent
    # builds a thin breakdown from a Confluence link it never opened, and the
    # requirement gets blamed for the gap.
    if context_notes:
        blocks.append(("What I could not read", [n for n in context_notes if n]))
    # A linked page is usually newer than the ticket pointing at it, so the two
    # disagreeing is ordinary. Silently preferring one is what is not.
    if conflicts:
        blocks.append(("Conflicting information", [c for c in conflicts if c]))
    if notified:
        blocks.append(("Notification", notified))
    # The footer is what stops the agent answering its own reply. It contains
    # no bare "Aetherion", so even without the signature check the comment
    # cannot read as a mention.
    blocks.append(("", AGENT_FOOTER))
    return adf(blocks)
