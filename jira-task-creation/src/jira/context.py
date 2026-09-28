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

import os
from typing import Any

import httpx
from common_lib.utils.logger import setup_logger

from src.jira.adf import _plain_text
from src.jira.client import (
    _http_error_text,
    _quote,
    browse_url,
    issue_type_names,
)
from src.models.schemas import (
    EpicContext,
    ExistingIssue,
    ProjectInfo,
    SprintInfo,
)

logger = setup_logger(__name__)


# --------------------------------------------------------------------------
# Reading project context: description, existing tickets, epics, sprints
# --------------------------------------------------------------------------


async def is_authenticated(client: httpx.AsyncClient) -> bool:
    """True when the credentials are actually accepted by Jira.

    Needed because Jira Cloud hides project existence from unauthenticated
    callers: ``GET /project/{key}`` answers **404** for a bad token, exactly as
    it does for a project that is not there. Only ``/myself`` returns the 401,
    so a 404 has to be disambiguated against it or an expired token gets
    reported as a missing project.
    """
    try:
        resp = await client.get("/rest/api/3/myself")
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


async def fetch_project(client: httpx.AsyncClient, project_key: str) -> ProjectInfo:
    """Read the project itself, so context comes from Jira rather than the form.

    Raises ``httpx.HTTPStatusError`` for an unknown or invisible project, which
    the caller turns into a clear refusal.
    """
    resp = await client.get(f"/rest/api/3/project/{project_key}")
    resp.raise_for_status()
    data = resp.json()
    return ProjectInfo(
        key=data.get("key", project_key),
        id=str(data.get("id", "")),
        name=data.get("name", "") or "",
        description=_plain_text(data.get("description")),
        style=data.get("style", "") or "",
    )


# How much existing work to read before deciding anything. Jira caps a single
# page at 100, so anything above that is paged through.
DEFAULT_MAX_CONTEXT_ISSUES = 300
# Existing descriptions are only used for overlap scoring, so a snippet is
# enough — carrying whole descriptions for 300 tickets would be wasteful.
_DESCRIPTION_SNIPPET = 1500


def max_context_issues() -> int:
    """How many existing tickets to read, from ``LTW_MAX_CONTEXT_ISSUES``."""
    raw = os.environ.get("LTW_MAX_CONTEXT_ISSUES", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_CONTEXT_ISSUES
    except ValueError:
        logger.warning(f"LTW_MAX_CONTEXT_ISSUES is not an integer ({raw!r}); using default")
        value = DEFAULT_MAX_CONTEXT_ISSUES
    return max(1, value)


async def fetch_existing_issues(
    client: httpx.AsyncClient,
    project_key: str,
    limit: int | None = None,
    base_url: str = "",
) -> tuple[list[ExistingIssue], list[str]]:
    """Read the project's existing work, following every page.

    Jira returns at most 100 issues per request and hands back a
    ``nextPageToken`` for the rest, so this keeps asking until Jira says
    ``isLast`` or the configured cap is reached. Without paging, only the 100
    newest tickets were ever compared — older work was invisible to duplicate
    detection.

    Returns ``(issues, unavailable)``. ``unavailable`` describes anything that
    could not be read, so a partial context is reported rather than silently
    assumed complete.
    """
    cap = max_context_issues() if limit is None else max(1, limit)
    jql = f"project = {_quote(project_key)} ORDER BY created DESC"
    fields = "summary,issuetype,status,parent,labels,description"

    issues: list[ExistingIssue] = []
    unavailable: list[str] = []
    token: str | None = None
    pages = 0

    while len(issues) < cap:
        params: dict[str, Any] = {
            "jql": jql,
            "maxResults": min(100, cap - len(issues)),
            "fields": fields,
        }
        if token:
            params["nextPageToken"] = token
        try:
            resp = await client.get("/rest/api/3/search/jql", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # Keep what we already have; say plainly that the rest is missing.
            unavailable.append(
                f"Existing tickets after page {pages}: {exc}. "
                f"Duplicate detection saw only the {len(issues)} newest."
            )
            break

        payload = resp.json()
        pages += 1
        for item in payload.get("issues", []):
            f = item.get("fields", {})
            key = item.get("key", "")
            issues.append(
                ExistingIssue(
                    key=key,
                    summary=f.get("summary", "") or "",
                    issue_type=(f.get("issuetype") or {}).get("name", "") or "",
                    status=(f.get("status") or {}).get("name", "") or "",
                    status_category=(
                        ((f.get("status") or {}).get("statusCategory") or {}).get("key", "") or ""
                    ),
                    parent_key=(f.get("parent") or {}).get("key", "") or "",
                    labels=[str(x) for x in (f.get("labels") or [])],
                    description=_plain_text(f.get("description"))[:_DESCRIPTION_SNIPPET],
                    url=browse_url(base_url, key),
                )
            )

        token = payload.get("nextPageToken")
        if payload.get("isLast") or not token:
            break

    if token and not payload.get("isLast") and len(issues) >= cap:
        unavailable.append(
            f"Stopped after {len(issues)} existing tickets (LTW_MAX_CONTEXT_ISSUES). "
            f"Older work was not compared."
        )

    logger.info(f"Read {len(issues)} existing ticket(s) from {project_key} over {pages} page(s)")
    return issues, unavailable


class EpicValidationError(RuntimeError):
    """The supplied epic key cannot be used, with a reason fit to show a user."""


async def fetch_epic(client: httpx.AsyncClient, epic_key: str, project_key: str) -> EpicContext:
    """Validate an existing epic key and read it, plus the stories already on it.

    Three separate failure modes, each with its own message: the issue does not
    exist, it exists but is not an Epic, or it belongs to a different project.
    """
    key = epic_key.strip().upper()
    try:
        resp = await client.get(
            f"/rest/api/3/issue/{key}",
            params={"fields": "summary,description,issuetype,project"},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise EpicValidationError(
                f"Epic '{key}' was not found, or is not visible to this account."
            ) from exc
        if exc.response.status_code in (401, 403):
            raise EpicValidationError(f"This account is not permitted to read '{key}'.") from exc
        raise EpicValidationError(f"Could not read '{key}': {_http_error_text(exc)}") from exc

    fields = resp.json().get("fields", {})
    type_name = (fields.get("issuetype") or {}).get("name", "") or ""
    owning = (fields.get("project") or {}).get("key", "") or ""
    expected_epic = issue_type_names()["epic"]

    if owning.upper() != project_key.strip().upper():
        raise EpicValidationError(
            f"Epic '{key}' belongs to project '{owning}', not '{project_key}'. "
            f"Choose an epic from '{project_key}' or change the project key."
        )
    if type_name.strip().lower() != expected_epic.strip().lower():
        raise EpicValidationError(
            f"'{key}' is a {type_name or 'unknown type'}, not an {expected_epic}. "
            f"Supply the key of an {expected_epic} to add stories to."
        )

    children = await fetch_epic_children(client, key, project_key)
    return EpicContext(
        key=key,
        summary=fields.get("summary", "") or "",
        description=_plain_text(fields.get("description")),
        child_stories=children,
    )


async def fetch_epic_children(
    client: httpx.AsyncClient, epic_key: str, project_key: str
) -> list[ExistingIssue]:
    """Children already hanging off a parent, so new ones do not repeat them.

    Used for an Epic's Stories and for a ticket's Sub-tasks; the JQL is the
    same either way.
    """
    jql = f"project = {_quote(project_key)} AND parent = {_quote(epic_key)}"
    resp = await client.get(
        "/rest/api/3/search/jql",
        params={"jql": jql, "maxResults": 100, "fields": "summary,issuetype,status,parent"},
    )
    resp.raise_for_status()
    out: list[ExistingIssue] = []
    for item in resp.json().get("issues", []):
        f = item.get("fields", {})
        out.append(
            ExistingIssue(
                key=item.get("key", ""),
                summary=f.get("summary", "") or "",
                issue_type=(f.get("issuetype") or {}).get("name", "") or "",
                status=(f.get("status") or {}).get("name", "") or "",
                status_category=(
                    ((f.get("status") or {}).get("statusCategory") or {}).get("key", "") or ""
                ),
                parent_key=(f.get("parent") or {}).get("key", "") or "",
            )
        )
    return out


# --------------------------------------------------------------------------
# Boards and sprints (Jira Agile API)
# --------------------------------------------------------------------------


class SprintUnavailable(RuntimeError):
    """No usable active sprint, with a reason fit to show a user."""


async def resolve_board(client: httpx.AsyncClient, project_key: str) -> tuple[int, str]:
    """Find the board for a project.

    ``JIRA_BOARD_ID`` pins the choice. Otherwise a single board is used
    automatically and several boards is an error the user must resolve — the
    agent will not guess which board owns the sprint.
    """
    pinned = os.environ.get("JIRA_BOARD_ID", "").strip()
    if pinned:
        try:
            return int(pinned), f"board {pinned} (pinned by JIRA_BOARD_ID)"
        except ValueError as exc:
            raise SprintUnavailable(f"JIRA_BOARD_ID is not a number: {pinned!r}.") from exc

    resp = await client.get(
        "/rest/agile/1.0/board", params={"projectKeyOrId": project_key, "maxResults": 50}
    )
    resp.raise_for_status()
    boards = resp.json().get("values", [])
    if not boards:
        raise SprintUnavailable(
            f"Project '{project_key}' has no board, so it has no sprints. "
            f"Stories will stay in the backlog."
        )
    if len(boards) > 1:
        listed = ", ".join(f"{b.get('id')}:{b.get('name')}" for b in boards[:8])
        raise SprintUnavailable(
            f"Project '{project_key}' has {len(boards)} boards ({listed}). "
            f"Set JIRA_BOARD_ID to the one whose sprint should be used."
        )
    return int(boards[0]["id"]), boards[0].get("name", "") or ""


async def active_sprint(client: httpx.AsyncClient, project_key: str) -> SprintInfo:
    """Resolve the single active sprint for the project's board.

    Raises ``SprintUnavailable`` when there is no board, no active sprint, or
    an ambiguous choice — never silently picks one.
    """
    board_id, board_name = await resolve_board(client, project_key)
    resp = await client.get(
        f"/rest/agile/1.0/board/{board_id}/sprint",
        params={"state": "active", "maxResults": 50},
    )
    if resp.status_code == 400:
        raise SprintUnavailable(
            f"Board {board_id} does not support sprints (it is probably a Kanban "
            f"board). Stories will stay in the backlog."
        )
    resp.raise_for_status()
    sprints = resp.json().get("values", [])
    if not sprints:
        raise SprintUnavailable(
            f"There is no active sprint on board {board_id}"
            f"{' (' + board_name + ')' if board_name else ''}. "
            f"Start a sprint in Jira, or choose backlog placement."
        )
    if len(sprints) > 1:
        listed = ", ".join(f"{s.get('id')}:{s.get('name')}" for s in sprints[:8])
        raise SprintUnavailable(
            f"Board {board_id} has {len(sprints)} active sprints ({listed}). "
            f"Jira cannot tell which one to use; close one or choose backlog."
        )
    sprint = sprints[0]
    return SprintInfo(
        id=int(sprint["id"]),
        name=sprint.get("name", "") or "",
        board_id=board_id,
        board_name=board_name,
    )


async def add_to_sprint(client: httpx.AsyncClient, sprint_id: int, issue_keys: list[str]) -> None:
    """Move issues into a sprint. Subtasks follow their parent automatically."""
    if not issue_keys:
        return
    # The Agile API accepts at most 50 issues per call.
    for start in range(0, len(issue_keys), 50):
        chunk = issue_keys[start : start + 50]
        resp = await client.post(
            f"/rest/agile/1.0/sprint/{sprint_id}/issue", json={"issues": chunk}
        )
        resp.raise_for_status()
