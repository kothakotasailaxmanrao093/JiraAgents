"""Typed Jira client for the requirement-review agents.

Wraps the HTTP helpers with the exact operations this feature needs:
fetch a single issue (with description, parent, subtasks, comments), resolve the
parent and subtasks into compact "bundles", and post a comment (the only write).

The client takes an injected ``aiohttp`` session so it can be unit-tested against
a fake transport. ``jira_client_from_env`` is the production composition root.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from config import (
    MAX_ATTACHMENTS,
    MAX_CHARS_PER_COMMENT,
    MAX_COMMENTS_PER_ISSUE,
    MAX_DESC_CHARS,
    MAX_JQL_RESULTS,
    MAX_LINKED_ISSUES,
    MAX_SUBTASKS,
    JiraSettings,
)

from . import http
from .transport import AiohttpTransport

logger = logging.getLogger(__name__)

# Fields the review needs (the dashboard tool's default fields omit these).
# "attachment" is here because the specification is routinely a file hanging off
# the ticket rather than prose in the description.
ISSUE_FIELDS = [
    "summary",
    "description",
    "parent",
    "subtasks",
    "comment",
    "issuelinks",
    "attachment",
    # So a Sub-task is reviewed as a Sub-task (BGV-32, 2026-09-25).
    "issuetype",
]


class JiraClient:
    def __init__(self, settings: JiraSettings, session: aiohttp.ClientSession | Any):
        self._settings = settings
        self._session = session

    @property
    def base(self) -> str:
        return self._settings.base_url

    @property
    def transport(self) -> AiohttpTransport:
        """This client's session, behind the shared transport port.

        Shared code (attachment downloads, Confluence reading) depends on the
        port rather than on ``aiohttp``, so it works identically for the sibling
        agent built on ``httpx``. Building it here reuses the authenticated
        session instead of opening a second one.
        """
        return AiohttpTransport(self._session)

    async def preflight(self) -> dict | None:
        """Confirm the token authenticates (and log who) before any read/write."""
        url = f"{self.base}{http.JIRA_API}/myself"
        try:
            data = await http.get_json(self._session, url)
            logger.info(
                "Jira auth OK — %s <%s> accountId=%s",
                data.get("displayName"),
                data.get("emailAddress"),
                data.get("accountId"),
            )
            return data
        except Exception as e:  # diagnostic only — never abort here
            logger.error("Jira preflight failed at %s: %s", url, e)
            return None

    async def get_issue(self, key: str, fields: list[str] | None = None) -> dict:
        params = {"fields": ",".join(fields or ISSUE_FIELDS)}
        return await http.get_json(self._session, f"{self.base}{http.JIRA_API}/issue/{key}", params)

    def extract_bundle(self, issue: dict) -> dict:
        """Reduce a raw issue to the compact shape downstream needs (capped)."""
        fields = issue.get("fields") or {}

        description_text = http.adf_to_text(fields.get("description"))[:MAX_DESC_CHARS]

        comment_block = fields.get("comment") or {}
        raw_comments = comment_block.get("comments", []) if isinstance(comment_block, dict) else []
        comments: list[dict[str, Any]] = []
        for c in raw_comments[
            -MAX_COMMENTS_PER_ISSUE:
        ]:  # most recent N (Jira returns oldest-first)
            author = (c.get("author") or {}).get("displayName")
            body = http.adf_to_text(c.get("body"))[:MAX_CHARS_PER_COMMENT]
            if body:
                comments.append(
                    {
                        # The id is what lets the webhook path find the comment
                        # that actually asked for the review.
                        "id": str(c.get("id") or ""),
                        "author": author,
                        "created": c.get("created"),
                        "body": body,
                    }
                )

        parent = fields.get("parent") or {}
        subtasks = fields.get("subtasks") or []
        subtask_keys = [s.get("key") for s in subtasks if isinstance(s, dict) and s.get("key")]

        linked_issues: list[dict[str, Any]] = []
        for link in fields.get("issuelinks") or []:
            link_type = ((link.get("type") or {}).get("name")) or "relates to"
            outward = link.get("outwardIssue")
            inward = link.get("inwardIssue")
            target = outward or inward
            if not isinstance(target, dict) or not target.get("key"):
                continue
            linked_issues.append(
                {
                    "key": target.get("key"),
                    "summary": (target.get("fields") or {}).get("summary"),
                    "link_type": link_type,
                    "direction": "outward" if outward else "inward",
                }
            )

        attachments: list[dict[str, Any]] = []
        for att in fields.get("attachment") or []:
            if not isinstance(att, dict) or not att.get("filename"):
                continue
            attachments.append(
                {
                    "filename": att.get("filename"),
                    "mime_type": att.get("mimeType") or "",
                    "size": att.get("size") or 0,
                    "content_url": att.get("content") or "",
                }
            )

        return {
            "key": issue.get("key"),
            "summary": fields.get("summary"),
            "description_text": description_text,
            "comments": comments,
            "parent_key": parent.get("key"),
            "is_subtask": bool((fields.get("issuetype") or {}).get("subtask")),
            "subtask_total": len(subtask_keys),  # full count, before the cap
            "subtask_keys": subtask_keys[:MAX_SUBTASKS],
            "linked_issues_total": len(linked_issues),  # full count, before the cap
            "linked_issues": linked_issues[:MAX_LINKED_ISSUES],
            "attachments_total": len(attachments),  # full count, before the cap
            "attachments": attachments[:MAX_ATTACHMENTS],
        }

    async def get_issue_bundle(self, key: str) -> dict:
        return self.extract_bundle(await self.get_issue(key))

    async def get_parent_bundle(self, parent_key: str | None) -> dict | None:
        if not parent_key:
            return None
        return self.extract_bundle(await self.get_issue(parent_key))

    async def get_subtask_bundles(self, subtask_keys: list[str]) -> list[dict]:
        out: list[dict] = []
        for key in (subtask_keys or [])[:MAX_SUBTASKS]:
            try:
                out.append(self.extract_bundle(await self.get_issue(key)))
            except Exception as e:  # one bad subtask should not sink the rest
                logger.warning("Subtask %s fetch failed: %s", key, e)
        return out

    async def search_jql(self, jql: str, max_results: int | None = None) -> list[str]:
        """Resolve a JQL/Sprint query to a bounded list of issue keys.

        Uses the current ``/search/jql`` endpoint, falling back to the legacy
        ``/search`` endpoint on 404/410 (mirrors ``jira_fetch_card``'s approach
        in the pre-groom agent this batch mode was merged from).
        """
        cap = max_results or MAX_JQL_RESULTS
        url = f"{self.base}{http.JIRA_API}/search/jql"
        body = {"jql": jql, "maxResults": cap, "fields": ["key"]}
        try:
            data = await http.post_json(self._session, url, body)
        except RuntimeError as e:
            if "404" in str(e) or "410" in str(e):
                legacy_url = f"{self.base}{http.JIRA_API}/search"
                data = await http.post_json(self._session, legacy_url, body)
            else:
                raise
        issues = data.get("issues", []) if isinstance(data, dict) else []
        return [i.get("key") for i in issues if isinstance(i, dict) and i.get("key")]

    async def post_comment(self, key: str, adf_doc: dict) -> dict:
        url = f"{self.base}{http.JIRA_API}/issue/{key}/comment"
        result = await http.post_json(self._session, url, {"body": adf_doc})
        logger.info("Posted comment %s to %s", result.get("id"), key)
        return result

    def browse_url(self, key: str, comment_id: str | None = None) -> str:
        url = f"{self.base}/browse/{key}"
        return f"{url}?focusedCommentId={comment_id}" if comment_id else url


@contextlib.asynccontextmanager
async def jira_client_from_env() -> AsyncIterator[JiraClient]:
    """Production composition root: build a session with BasicAuth from env."""
    settings = JiraSettings.from_env()
    auth = aiohttp.BasicAuth(settings.email, settings.api_token)
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(auth=auth, timeout=timeout) as session:
        yield JiraClient(settings, session)
