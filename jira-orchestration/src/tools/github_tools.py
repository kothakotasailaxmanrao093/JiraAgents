"""@tools: what changed on GitHub, as input for the Planning agent (2026-10-03).

The network, the clock and the environment live here; which changes count, and
how they are written out, live in ``routing/github_events.py``.

The memory is kept on a Jira project, as properties (invisible to users):
``aetherion-github-poll`` (named when GitHub was still polled; kept so nothing
already sent is sent again) holds what was already sent; each event waiting to
be sent — or to be retried after a failed Planning run — has its own property,
so a long PR conversation never outgrows Jira's 32 KB per property. An event is
stored *before* it is handed out, so a run that dies half-way loses nothing: the
next webhook event finds it waiting.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import httpx
from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger

from routing import github_events as gh
from routing import settings
from tools import org_repos
from tools.tools import _client

logger = setup_logger(__name__)

GITHUB_API = "https://api.github.com"
_PENDING_PREFIX = "aetherion-gh-"
_MAX_PAGES = 3


def _github() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=GITHUB_API,
        timeout=30.0,
        headers={
            "Authorization": f"Bearer {settings.github_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _not_configured() -> str:
    missing = [
        name
        for name, value in (
            ("GITHUB_TOKEN", settings.github_token()),
            # An organization to watch, or the repositories Planning designs against.
            ("ORCH_GITHUB_ORG", settings.github_org() or settings.planning_repos()),
            ("ORCH_GITHUB_STATE_PROJECT", settings.github_state_project()),
        )
        if not value
    ]
    return f"GitHub events are not configured: set {', '.join(missing)}." if missing else ""


# --- the memory on the Jira project ---------------------------------------------


def _property(name: str) -> str:
    return f"/rest/api/3/project/{settings.github_state_project()}/properties/{name}"


def pending_property(key: str) -> str:
    return _PENDING_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:24]


async def _get(jira: httpx.AsyncClient, name: str) -> dict[str, Any]:
    resp = await jira.get(_property(name))
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    value = resp.json().get("value")
    return value if isinstance(value, dict) else {}


async def _put(jira: httpx.AsyncClient, name: str, value: dict[str, Any]) -> None:
    (await jira.put(_property(name), json=value)).raise_for_status()


async def _delete(jira: httpx.AsyncClient, name: str) -> None:
    resp = await jira.delete(_property(name))
    if resp.status_code != 404:
        resp.raise_for_status()


# --- reading GitHub ---------------------------------------------------------------


async def _pages(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> list[dict]:
    out: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        resp = await client.get(url, params={**params, "per_page": 100, "page": page})
        resp.raise_for_status()
        batch = resp.json()
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


async def _org_repos(client: httpx.AsyncClient) -> list[str]:
    """The organization's repositories, and every one Planning designs against.

    Planning's own repositories may live outside the organization (a personal
    account); a merge into any of them must still refresh the graph.
    """
    found: set[str] = set()
    if settings.github_org():
        repos = await _pages(client, f"/orgs/{settings.github_org()}/repos", {"type": "all"})
        wanted = settings.github_repos()
        found = {
            r["full_name"]
            for r in repos
            if not r.get("archived") and (not wanted or r["name"].lower() in wanted)
        }
    known = {f.lower() for f in found}
    found |= {s for s in gh.repo_slugs(settings.planning_repos()) if s.lower() not in known}
    return sorted(found)


async def _comments(
    client: httpx.AsyncClient, repo: str, number: int, since: datetime | None
) -> list[gh.Comment]:
    params = {"since": since.isoformat()} if since else {}
    issue = await _pages(client, f"/repos/{repo}/issues/{number}/comments", params)
    lines = await _pages(client, f"/repos/{repo}/pulls/{number}/comments", params)
    reviews = await _pages(client, f"/repos/{repo}/pulls/{number}/reviews", {})
    if since:
        reviews = [r for r in reviews if (gh.parse_time(r.get("submitted_at")) or since) >= since]
    return gh.comments_from(issue, lines, reviews)


# --- the tools --------------------------------------------------------------------


async def _store(jira: httpx.AsyncClient, found: list[gh.Event], pending: dict[str, int]) -> None:
    """Each new event saved as waiting before it is handed out, so none is lost."""
    for event in found:
        if event.key not in pending:
            event_dict = {**event.as_dict(), "workflow_id": _workflow_id(event.key)}
            await _put(jira, pending_property(event.key), event_dict)
            pending[event.key] = 0


async def _save_and_list(
    jira: httpx.AsyncClient, memory: dict[str, Any], pending: dict[str, int]
) -> list[dict[str, Any]]:
    """Save the memory; every waiting event — new ones and retries — to send now."""
    memory["pending"] = pending
    await _put(jira, settings.GITHUB_STATE_PROPERTY, memory)
    events = []
    for key, tries in pending.items():
        stored = await _get(jira, pending_property(key))
        if stored:
            events.append({**stored, "tries": tries})
    return events


async def _scan_result(
    events: list[dict[str, Any]], repos: int, notes: list[str]
) -> dict[str, Any]:
    return {
        "ok": True,
        "events": events,
        "repos": repos,
        "notes": notes,
        "planning_agent": settings.planning_agent(),
        "planning_queue": settings.planning_task_queue(),
        # Which service each repository is, for Planning's graph.
        "repos_text": await org_repos.planning_repos(),
        "timeout_minutes": settings.planning_timeout_minutes(),
        # A merge goes into the project's history, kept by the Requirement
        # Review agent (2026-10-30), on its own task queue.
        "handoff": settings.memory_enabled() and settings.handoff_enabled(),
        "review_queue": settings.task_queue_overrides().get("JiraRequirementReview", ""),
    }


# A pushed comment's kind -> where GitHub keeps that one comment.
_COMMENT_URLS = {
    "issue": "/repos/{repo}/issues/comments/{id}",
    "line": "/repos/{repo}/pulls/comments/{id}",
    "review": "/repos/{repo}/pulls/{number}/reviews/{id}",
}
_COMMENT_KINDS = {
    "issue_comment": "issue",
    "pull_request_review_comment": "line",
    "pull_request_review": "review",
}


async def _pushed_events(
    client: httpx.AsyncClient, fields: dict[str, Any], sent: dict[str, str]
) -> tuple[list[gh.Event], str]:
    """(the events one pushed GitHub webhook stands for, or why there are none).

    The webhook only says what happened; the pull request, its files and its
    comments are read from GitHub."""
    repo = str(fields.get("github_repo") or "")
    kind = str(fields.get("github_event") or "")
    action = str(fields.get("github_action") or "")
    number = int(fields.get("pr_number") or 0)
    if not repo or not number:
        return [], "not about a pull request"
    watched = {r.lower() for r in await _org_repos(client)}
    if repo.lower() not in watched:
        return [], f"{repo} is not a watched repository"

    resp = await client.get(f"/repos/{repo}/pulls/{number}")
    resp.raise_for_status()
    pr = resp.json()
    projects = settings.allowed_project_keys()

    if kind == "pull_request":
        branch = settings.github_branch()
        if action != "closed" or not pr.get("merged_at"):
            return [], f"pull request {action or 'event'} is not a merge"
        if (pr.get("base") or {}).get("ref") != branch:
            return [], f"merged into {(pr.get('base') or {}).get('ref')}, not {branch}"
        if gh.merge_key(repo, pr) in sent:
            return [], "this merge was already sent"
        files = await _pages(client, f"/repos/{repo}/pulls/{number}/files", {})
        comments = await _comments(client, repo, number, None)
        return [gh.merged_event(repo, pr, files, comments, projects)], ""

    comment_kind = _COMMENT_KINDS.get(kind)
    comment_id = str(fields.get("github_comment_id") or "")
    if comment_kind is None or not comment_id:
        return [], f"{kind or 'an event'} is not handled"
    if action not in ("created", "submitted"):
        return [], f"a comment {action} is not new"
    url = _COMMENT_URLS[comment_kind].format(repo=repo, id=comment_id, number=number)
    resp = await client.get(url)
    resp.raise_for_status()
    raw = resp.json()
    found = gh.comments_from(
        [raw] if comment_kind == "issue" else [],
        [raw] if comment_kind == "line" else [],
        [raw] if comment_kind == "review" else [],
    )
    new = [c for c in found if gh.comment_key(repo, number, c) not in sent]
    if not new:
        return [], "a bot's, empty or already sent comment"
    return [gh.comments_event(repo, pr, new, projects)], ""


@tool(name="github_event")
async def github_event(fields: dict[str, Any]) -> dict[str, Any]:
    """One GitHub webhook event, pushed as it happened (2026-10-14): its events,
    plus any still waiting for a retry. ``fields`` are the webhook's flat ones:
    github_event, github_action, github_repo, pr_number, github_comment_id."""
    problem = _not_configured()
    if problem:
        return {"ok": False, "error": problem, "events": []}
    now = datetime.now(UTC)
    try:
        async with _client() as jira, _github() as client:
            memory = await _get(jira, settings.GITHUB_STATE_PROPERTY)
            sent = gh.prune(memory.get("sent") or {}, now)
            pending: dict[str, int] = memory.get("pending") or {}
            found, skipped = await _pushed_events(client, fields, sent)
            await _store(jira, found, pending)
            memory["sent"] = sent
            events = await _save_and_list(jira, memory, pending)
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(f"GitHub event failed: {exc}", exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "events": []}

    logger.info(f"GitHub event: {len(found)} new, {len(events)} to send; {skipped or 'taken'}")
    result = await _scan_result(events, 1, [skipped] if skipped else [])
    result["skipped"] = skipped
    return result


def _workflow_id(key: str) -> str:
    """The Planning run's id: the same event never runs twice at once."""
    return "planning-gh-" + hashlib.sha256(key.encode()).hexdigest()[:20]


@tool(name="github_event_done")
async def github_event_done(key: str, ok: bool, message: str = "") -> dict[str, Any]:
    """Record a Planning run's outcome: sent, or kept for the next check's retry."""
    now = datetime.now(UTC).isoformat()
    try:
        async with _client() as jira:
            memory = await _get(jira, settings.GITHUB_STATE_PROPERTY)
            pending: dict[str, int] = memory.get("pending") or {}
            event = await _get(jira, pending_property(key))
            gave_up = False
            if ok:
                sent = memory.get("sent") or {}
                for mark in event.get("marks") or [key]:
                    sent[mark] = now
                memory["sent"] = sent
                pending.pop(key, None)
            else:
                tries = pending.get(key, 0) + 1
                gave_up = tries >= settings.github_max_tries()
                if gave_up:
                    pending.pop(key, None)
                else:
                    pending[key] = tries
            if key not in pending:
                await _delete(jira, pending_property(key))
            memory["pending"] = pending
            await _put(jira, settings.GITHUB_STATE_PROPERTY, memory)
    except httpx.HTTPError as exc:
        logger.error(f"Could not record the outcome of {key}: {exc}", exc_info=True)
        return {"recorded": False, "error": str(exc)}
    if not ok:
        logger.warning(f"Planning run for {key} failed ({message}); gave_up={gave_up}")
    return {"recorded": True, "gave_up": gave_up}
