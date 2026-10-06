"""@tools: what changed on GitHub, as input for the Planning agent (2026-10-03).

The network, the clock and the environment live here; which changes count, and
how they are written out, live in ``routing/github_events.py``.

The memory is kept on a Jira project, as properties (invisible to users):
``aetherion-github-poll`` holds how far each repo was read and what was already
sent; each event waiting to be sent — or to be retried after a failed Planning
run — has its own property, so a long PR conversation never outgrows Jira's
32 KB per property. An event is stored *before* it is handed out, so a check
that dies half-way loses nothing: the next one finds it waiting.
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
            ("ORCH_GITHUB_ORG", settings.github_org()),
            ("ORCH_GITHUB_STATE_PROJECT", settings.github_state_project()),
        )
        if not value
    ]
    return f"GitHub polling is not configured: set {', '.join(missing)}." if missing else ""


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
    repos = await _pages(client, f"/orgs/{settings.github_org()}/repos", {"type": "all"})
    wanted = settings.github_repos()
    return sorted(
        r["full_name"]
        for r in repos
        if not r.get("archived") and (not wanted or r["name"].lower() in wanted)
    )


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


async def _repo_events(
    client: httpx.AsyncClient, repo: str, mine: dict[str, Any], sent: dict[str, str], now: datetime
) -> list[gh.Event]:
    """This repo's new events since ``mine``'s cursor; moves the cursor on."""
    since = (gh.parse_time(mine.get("cursor")) or now) - gh.OVERLAP
    headers = {"If-None-Match": mine["etag"]} if mine.get("etag") else {}
    resp = await client.get(
        f"/repos/{repo}/pulls",
        params={"state": "all", "sort": "updated", "direction": "desc", "per_page": 50},
        headers=headers,
    )
    if resp.status_code == 304:  # no pull request changed (and the call is free)
        mine["cursor"] = now.isoformat()
        return []
    resp.raise_for_status()
    mine["etag"] = resp.headers.get("ETag", "")

    events: list[gh.Event] = []
    for pr in resp.json():
        if (gh.parse_time(pr.get("updated_at")) or now) < since:
            break  # sorted newest first: the rest did not change
        number = pr["number"]
        if gh.merged_into(pr, settings.github_branch(), since):
            if gh.merge_key(repo, pr) in sent:
                continue
            files = await _pages(client, f"/repos/{repo}/pulls/{number}/files", {})
            comments = await _comments(client, repo, number, None)
            events.append(
                gh.merged_event(repo, pr, files, comments, settings.allowed_project_keys())
            )
            continue
        new = [
            c
            for c in await _comments(client, repo, number, since)
            if gh.comment_key(repo, number, c) not in sent
        ]
        if new:
            events.append(gh.comments_event(repo, pr, new, settings.allowed_project_keys()))
    mine["cursor"] = now.isoformat()
    return events


# --- the tools --------------------------------------------------------------------


@tool(name="github_scan")
async def github_scan() -> dict[str, Any]:
    """New merges into the branch and new PR comments, plus events waiting for a
    retry — each stored before it is returned, so none can be lost."""
    problem = _not_configured()
    if problem:
        return {"ok": False, "error": problem, "events": []}
    now = datetime.now(UTC)
    notes: list[str] = []
    try:
        async with _client() as jira, _github() as client:
            memory = await _get(jira, settings.GITHUB_STATE_PROPERTY)
            repos_memory: dict[str, Any] = memory.setdefault("repos", {})
            sent = gh.prune(memory.get("sent") or {}, now)
            pending: dict[str, int] = memory.get("pending") or {}  # key -> tries so far

            repos = await _org_repos(client)
            for repo in repos:
                if repo not in repos_memory:
                    # First sight of this repo: start from now, never its history.
                    repos_memory[repo] = {"cursor": now.isoformat()}
                    notes.append(f"{repo}: watching from now on")
                    continue
                try:
                    found = await _repo_events(client, repo, repos_memory[repo], sent, now)
                except httpx.HTTPError as exc:
                    notes.append(f"{repo}: not read this time ({exc})")  # cursor stays put
                    continue
                for event in found:
                    if event.key not in pending:
                        event_dict = {**event.as_dict(), "workflow_id": _workflow_id(event.key)}
                        await _put(jira, pending_property(event.key), event_dict)
                        pending[event.key] = 0

            memory["sent"] = sent
            memory["pending"] = pending
            await _put(jira, settings.GITHUB_STATE_PROPERTY, memory)
            events = []
            for key, tries in pending.items():
                stored = await _get(jira, pending_property(key))
                if stored:
                    events.append({**stored, "tries": tries})
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(f"GitHub scan failed: {exc}", exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "events": []}

    logger.info(f"GitHub scan: {len(repos)} repo(s), {len(events)} event(s) to send")
    return {
        "ok": True,
        "events": events,
        "repos": len(repos),
        "notes": notes,
        "planning_agent": settings.planning_agent(),
        "planning_queue": settings.planning_task_queue(),
        "timeout_minutes": settings.planning_timeout_minutes(),
    }


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
