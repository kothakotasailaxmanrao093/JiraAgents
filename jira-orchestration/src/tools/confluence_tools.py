"""@tools for the TDD, design and code commands: Jira and Confluence (2026-10-08).

What the workflow may not do itself — read and write Jira and Confluence, and
ask GitHub for a repository's default branch. The order of the steps is in
``agent/command_flow.py``; the rules in ``routing/commands.py``.

Where things are remembered, all invisible to people:

* the Jira story's property ``aetherion-tdd`` — which Confluence page is its TDD;
* the page's property ``aetherion-story`` — which story it designs;
* the page's property ``aetherion-answered-comments`` — comments already answered;
* the page's property ``aetherion-code-plan`` — the branches proposed for coding;
* the label ``aetherion-tdd`` on every TDD page — what the comment check reads.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx
from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger

from routing import commands, settings
from routing import github_events as gh
from shared.adf import blocks_to_doc
from shared.confluence_text import storage_to_text
from shared.keywords import AGENT_FOOTER, is_agent_comment, mentions_trigger
from tools import org_repos
from tools.org_repos import with_repos
from tools.tools import _client

logger = setup_logger(__name__)

_TIMEOUT = 30.0
_STORY_FIELDS = "summary,issuetype,parent,project,subtasks,status"
_MAX_DISCUSSION = 30


def _wiki() -> httpx.AsyncClient:
    base = settings.confluence_base()
    email, token = settings.confluence_credentials()
    if not (base and email and token):
        raise ValueError(
            "Missing Confluence configuration: set JIRA_BASE_URL (or ORCH_CONFLUENCE_URL)."
        )
    return httpx.AsyncClient(
        base_url=base,
        auth=(email, token),
        timeout=_TIMEOUT,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )


def _page_url(data: dict[str, Any]) -> str:
    links = data.get("_links") or {}
    base = links.get("base") or settings.confluence_base()
    return f"{base}{links.get('webui', '')}" if links.get("webui") else ""


# --- the story ------------------------------------------------------------------------


async def _issue_property(jira: httpx.AsyncClient, key: str, name: str) -> dict[str, Any]:
    resp = await jira.get(f"/rest/api/3/issue/{key}/properties/{name}")
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    value = resp.json().get("value")
    return value if isinstance(value, dict) else {}


@tool(name="story_context")
async def story_context(issue_key: str) -> dict[str, Any]:
    """The story a command was asked on: its type, project, sub-tasks and TDD page."""
    issue_key = issue_key.strip().upper()
    try:
        async with _client() as jira:
            resp = await jira.get(
                f"/rest/api/3/issue/{issue_key}", params={"fields": _STORY_FIELDS}
            )
            resp.raise_for_status()
            fields = resp.json().get("fields") or {}
            tdd = await _issue_property(jira, issue_key, settings.TDD_PROPERTY)
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": f"Could not read {issue_key}: {exc}"}
    issuetype = fields.get("issuetype") or {}
    project = fields.get("project") or {}
    base, _, _ = settings.jira_credentials()
    return {
        "ok": True,
        "key": issue_key,
        "url": f"{base}/browse/{issue_key}",
        "summary": fields.get("summary") or "",
        "type": issuetype.get("name") or "",
        "is_subtask": bool(issuetype.get("subtask")),
        "parent_key": (fields.get("parent") or {}).get("key") or "",
        "project_key": project.get("key") or issue_key.split("-")[0],
        "project_name": project.get("name") or "",
        "subtask_keys": [s.get("key") for s in fields.get("subtasks") or [] if s.get("key")],
        "tdd": tdd,
    }


@tool(name="story_comment")
async def story_comment(issue_key: str, blocks: list[Any], run_id: str) -> dict[str, Any]:
    """A comment on the story — the PR link, a merge — signed, so it never retriggers."""
    if settings.read_only():
        return {"posted": False, "read_only": True}
    issue_key = issue_key.strip().upper()
    body = blocks_to_doc([(str(b[0]), b[1]) for b in blocks])
    try:
        async with _client() as jira:
            resp = await jira.post(f"/rest/api/3/issue/{issue_key}/comment", json={"body": body})
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(f"run {run_id}: could not comment on {issue_key}: {exc}")
        return {"posted": False, "error": str(exc)}
    return {"posted": True, "comment_id": str(resp.json().get("id") or "")}


# --- the space and the page -----------------------------------------------------------


async def _space(wiki: httpx.AsyncClient, project_key: str, project_name: str) -> dict[str, Any]:
    """The project's space: configured, else matched by key or name, else created."""
    chosen = settings.confluence_spaces().get(project_key.upper())
    if chosen:
        resp = await wiki.get(f"/rest/api/space/{chosen}")
        resp.raise_for_status()
        return {"space_key": chosen, "space_name": resp.json().get("name", ""), "created": False}
    resp = await wiki.get("/rest/api/space", params={"limit": 250, "type": "global"})
    resp.raise_for_status()
    spaces = resp.json().get("results") or []
    for space in spaces:
        if space.get("key", "").upper() == project_key.upper():
            return {
                "space_key": space["key"],
                "space_name": space.get("name", ""),
                "created": False,
            }
    for space in spaces:
        if project_name and space.get("name", "").strip().lower() == project_name.strip().lower():
            return {
                "space_key": space["key"],
                "space_name": space.get("name", ""),
                "created": False,
            }
    key = re.sub(r"[^A-Za-z0-9]", "", project_key).upper() or "TDD"
    name = project_name or project_key
    resp = await wiki.post(
        "/rest/api/space",
        json={
            "key": key,
            "name": name,
            "description": {
                "plain": {
                    "value": f"Design documents for the Jira project {project_key}.",
                    "representation": "plain",
                }
            },
        },
    )
    resp.raise_for_status()
    logger.info(f"Created the Confluence space {key} ({name}) for {project_key}")
    return {"space_key": key, "space_name": name, "created": True}


async def _set_page_property(wiki: httpx.AsyncClient, page_id: str, key: str, value: dict) -> None:
    resp = await wiki.get(f"/rest/api/content/{page_id}/property/{key}")
    if resp.status_code == 404:
        resp = await wiki.post(
            f"/rest/api/content/{page_id}/property", json={"key": key, "value": value}
        )
    else:
        resp.raise_for_status()
        version = int(((resp.json().get("version") or {}).get("number")) or 1) + 1
        resp = await wiki.put(
            f"/rest/api/content/{page_id}/property/{key}",
            json={"key": key, "value": value, "version": {"number": version}},
        )
    resp.raise_for_status()


async def _page_property(wiki: httpx.AsyncClient, page_id: str, key: str) -> dict[str, Any]:
    resp = await wiki.get(f"/rest/api/content/{page_id}/property/{key}")
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    value = resp.json().get("value")
    return value if isinstance(value, dict) else {}


@tool(name="publish_tdd")
async def publish_tdd(
    issue_key: str,
    project_key: str,
    project_name: str,
    summary: str,
    title: str,
    html_body: str,
    run_id: str,
    related: list[dict[str, str]] | None = None,
    fingerprint: str = "",
) -> dict[str, Any]:
    """The TDD as a Confluence page in the project's space — a new version of the
    story's page when it has one — and the link kept on both sides. ``related``:
    the project's earlier designs, linked in the page's header. ``fingerprint``:
    the story it was written from; a revision asked on the page keeps the one
    already recorded, since the story did not change."""
    issue_key = issue_key.strip().upper()
    base, _, _ = settings.jira_credentials()
    story_url = f"{base}/browse/{issue_key}"
    keyword = settings.trigger_keyword()

    def content(page_version: int) -> dict[str, Any]:
        header = commands.tdd_page_header(
            issue_key,
            story_url,
            keyword,
            commands.tdd_version(page_version),
            summary or title,
            related,
        )
        return {"storage": {"value": header + html_body, "representation": "storage"}}

    try:
        async with _client() as jira, _wiki() as wiki:
            known = await _issue_property(jira, issue_key, settings.TDD_PROPERTY)
            space = await _space(wiki, project_key, project_name)
            page_id = str(known.get("page_id") or "")
            if page_id:
                current = await wiki.get(
                    f"/rest/api/content/{page_id}", params={"expand": "version"}
                )
                if current.status_code == 404:
                    page_id = ""  # deleted by someone: a new page is written
                else:
                    current.raise_for_status()
            if page_id:
                # The same page, revised: v1.0 -> v1.1 -> v1.2, history kept by Confluence.
                number = int(current.json()["version"]["number"]) + 1
                resp = await wiki.put(
                    f"/rest/api/content/{page_id}",
                    json={
                        "id": page_id,
                        "type": "page",
                        "title": commands.tdd_title(issue_key, number),
                        "version": {
                            "number": number,
                            "message": f"TDD {commands.tdd_version(number)} — revised by the "
                            f"Planning agent (run {run_id})",
                        },
                        "body": content(number),
                    },
                )
                created = False
            else:
                resp = await wiki.post(
                    "/rest/api/content",
                    json={
                        "type": "page",
                        "title": commands.tdd_title(issue_key, 1),
                        "space": {"key": space["space_key"]},
                        "body": content(1),
                    },
                )
                created = True
            resp.raise_for_status()
            page = resp.json()
            page_id = str(page["id"])
            url = _page_url(page)
            version = int((page.get("version") or {}).get("number") or 1)
            page_title = str(page.get("title") or commands.tdd_title(issue_key, version))
            await wiki.post(
                f"/rest/api/content/{page_id}/label",
                json=[{"prefix": "global", "name": settings.tdd_label()}],
            )
            await _set_page_property(
                wiki, page_id, settings.STORY_PROPERTY, {"issue_key": issue_key}
            )
            record = {
                "page_id": page_id,
                "url": url,
                "title": page_title,
                "space_key": space["space_key"],
                "version": version,
                "tdd_version": commands.tdd_version(version),
                "updated": datetime.now(UTC).isoformat(timespec="seconds"),
                "fingerprint": fingerprint or str(known.get("fingerprint") or ""),
            }
            put = await jira.put(
                f"/rest/api/3/issue/{issue_key}/properties/{settings.TDD_PROPERTY}", json=record
            )
            put.raise_for_status()
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.error(
            f"run {run_id}: could not publish the TDD for {issue_key}: {exc}", exc_info=True
        )
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    logger.info(f"run {run_id}: TDD for {issue_key} {'created' if created else 'updated'}: {url}")
    return {
        "ok": True,
        "created": created,
        "space_created": space["created"],
        **record,
        "space_name": space["space_name"],
    }


@tool(name="read_tdd")
async def read_tdd(page_id: str) -> dict[str, Any]:
    """The TDD page as it stands: its text, its story, the discussion and the code plan."""
    try:
        async with _wiki() as wiki:
            resp = await wiki.get(
                f"/rest/api/content/{page_id}", params={"expand": "body.storage,version"}
            )
            resp.raise_for_status()
            page = resp.json()
            story = await _page_property(wiki, page_id, settings.STORY_PROPERTY)
            plan = await _page_property(wiki, page_id, settings.CODE_PLAN_PROPERTY)
            comments = await _comments(wiki, page_id)
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": f"Could not read the TDD page {page_id}: {exc}"}
    storage = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
    return {
        "ok": True,
        "page_id": str(page_id),
        "title": page.get("title") or "",
        "url": _page_url(page),
        "version": int((page.get("version") or {}).get("number") or 1),
        "text": storage_to_text(storage),
        "issue_key": str(story.get("issue_key") or ""),
        "code_plan": plan,
        "discussion": [f"{c['author']}: {c['text']}" for c in comments][-_MAX_DISCUSSION:],
    }


# --- comments on the page ------------------------------------------------------------


async def _comments(wiki: httpx.AsyncClient, page_id: str) -> list[dict[str, Any]]:
    """Every comment on the page, replies included, oldest first."""
    resp = await wiki.get(
        f"/rest/api/content/{page_id}/child/comment",
        params={"expand": "body.storage,history,ancestors,version", "depth": "all", "limit": 200},
    )
    resp.raise_for_status()
    out = [_comment_fields(c) for c in resp.json().get("results") or []]
    return sorted(out, key=lambda c: c["created"])


def _comment_fields(c: dict[str, Any]) -> dict[str, Any]:
    """One Confluence comment as the flow reads it; a reply keeps its thread's root."""
    ancestors = [a for a in c.get("ancestors") or [] if a.get("type") == "comment"]
    return {
        "id": str(c["id"]),
        "root_id": str(ancestors[0]["id"]) if ancestors else str(c["id"]),
        "author": ((c.get("history") or {}).get("createdBy") or {}).get("displayName") or "Someone",
        "created": (c.get("history") or {}).get("createdDate") or "",
        "text": storage_to_text(((c.get("body") or {}).get("storage") or {}).get("value") or ""),
    }


# How much of the project's earlier designs a new design is given.
_EARLIER_DESIGNS = 5
_EARLIER_EXCERPT = 1500
# Where a TDD's requirements end, in its text: the section after them.
_AFTER_REQUIREMENTS = re.compile(r"\n\s*4\.\s+Current System|\n\s*Root Cause\b")


def _requirements_excerpt(text: str) -> str:
    """A design's requirements — what it promised — as its decisions in brief."""
    start = text.find("Requirements")
    body = text[start:] if start >= 0 else text
    end = _AFTER_REQUIREMENTS.search(body)
    body = body[: end.start()] if end else body
    body = " ".join(body.split())
    return body if len(body) <= _EARLIER_EXCERPT else body[: _EARLIER_EXCERPT - 1] + "…"


@tool(name="earlier_designs")
async def earlier_designs(issue_key: str) -> dict[str, Any]:
    """The project's other TDDs, newest first (2026-10-15): title, link, and what
    each promised. A new design is given them so it extends the project instead
    of describing only itself — Feature 2's TDD must not drop Feature 1's rules."""
    issue_key = issue_key.strip().upper()
    project = issue_key.split("-")[0]
    space = settings.confluence_spaces().get(project) or project
    cql = (
        f'label = "{settings.tdd_label()}" and type = page and space = "{space}" '
        "order by lastmodified desc"
    )
    try:
        async with _wiki() as wiki:
            resp = await wiki.get(
                "/rest/api/content/search",
                params={"cql": cql, "limit": 10, "expand": "body.storage"},
            )
            if resp.status_code == 404:
                return {"ok": True, "designs": []}
            resp.raise_for_status()
            pages = resp.json().get("results") or []
            base = str(resp.json().get("_links", {}).get("base") or settings.confluence_base())
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(f"Earlier designs for {issue_key} could not be read: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "designs": []}
    designs = []
    for page in pages:
        title = str(page.get("title") or "")
        if title.upper().startswith(f"{issue_key} - "):
            continue  # the story's own design is revised, not cited
        text = storage_to_text(((page.get("body") or {}).get("storage") or {}).get("value") or "")
        designs.append(
            {
                "title": title,
                "url": base + str((page.get("_links") or {}).get("webui") or ""),
                "excerpt": _requirements_excerpt(text),
            }
        )
        if len(designs) == _EARLIER_DESIGNS:
            break
    return {"ok": True, "designs": designs}


@tool(name="confluence_comment")
async def confluence_comment(page_id: str, comment_id: str) -> dict[str, Any]:
    """One comment, pushed by the Confluence webhook as it was posted (2026-10-14):
    that comment as the only event, or none and why. It must be on a TDD page,
    not answered yet, not one of the agent's own replies, and mention the keyword."""
    keyword = settings.trigger_keyword()
    base = {"pages": 1, "keyword": keyword, "settings": await with_repos(settings.commands())}
    try:
        async with _wiki() as wiki:
            resp = await wiki.get(
                f"/rest/api/content/{comment_id}",
                params={"expand": "body.storage,history,ancestors,container,version"},
            )
            if resp.status_code == 404:
                return {**base, "ok": True, "events": [], "skipped": "the comment is gone"}
            resp.raise_for_status()
            raw = resp.json()
            container = raw.get("container") or {}
            page_id = str(page_id or container.get("id") or "")
            if not page_id:
                return {**base, "ok": True, "events": [], "skipped": "no page for the comment"}
            labels = await wiki.get(f"/rest/api/content/{page_id}/label", params={"limit": 200})
            labels.raise_for_status()
            names = {lbl.get("name") for lbl in labels.json().get("results") or []}
            answered = set(
                (await _page_property(wiki, page_id, settings.ANSWERED_PAGE_PROPERTY)).get("ids")
                or []
            )
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(f"Confluence comment {comment_id} could not be read: {exc}", exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "events": []}

    comment = _comment_fields(raw)
    if settings.tdd_label() not in names:
        skipped = "not a TDD page"
    elif comment["id"] in answered:
        skipped = "already answered"
    elif is_agent_comment(comment["text"]):
        skipped = "the agent's own reply"
    elif not mentions_trigger(comment["text"], keyword=keyword):
        skipped = f"does not start with @{keyword}"
    else:
        skipped = ""
    events = (
        []
        if skipped
        else [{**comment, "page_id": page_id, "page_title": container.get("title") or ""}]
    )
    logger.info(f"Confluence comment {comment_id} on page {page_id}: {skipped or 'to answer'}")
    return {**base, "ok": True, "events": events, "skipped": skipped}


@tool(name="confluence_mark_answered")
async def confluence_mark_answered(page_id: str, comment_id: str) -> dict[str, Any]:
    try:
        async with _wiki() as wiki:
            known = await _page_property(wiki, page_id, settings.ANSWERED_PAGE_PROPERTY)
            ids = list(dict.fromkeys([*(known.get("ids") or []), str(comment_id)]))[-500:]
            await _set_page_property(wiki, page_id, settings.ANSWERED_PAGE_PROPERTY, {"ids": ids})
    except (httpx.HTTPError, ValueError) as exc:
        return {"recorded": False, "error": str(exc)}
    return {"recorded": True}


@tool(name="confluence_reply")
async def confluence_reply(
    page_id: str, parent_comment_id: str, blocks: list[Any], run_id: str
) -> dict[str, Any]:
    """A reply under a page comment (or a new comment when there is no parent)."""
    if settings.read_only():
        return {"posted": False, "read_only": True}
    body = commands.blocks_to_storage([(str(b[0]), b[1]) for b in blocks])
    payload: dict[str, Any] = {
        "type": "comment",
        "container": {"id": str(page_id), "type": "page"},
        "body": {"storage": {"value": body, "representation": "storage"}},
    }
    if parent_comment_id:
        payload["ancestors"] = [{"id": str(parent_comment_id)}]
    try:
        async with _wiki() as wiki:
            resp = await wiki.post("/rest/api/content", json=payload)
            if resp.status_code == 400 and parent_comment_id:
                payload.pop("ancestors")  # a reply to a reply: post it on the page instead
                resp = await wiki.post("/rest/api/content", json=payload)
            resp.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(f"run {run_id}: could not reply on page {page_id}: {exc}")
        return {"posted": False, "error": str(exc)}
    return {"posted": True, "comment_id": str(resp.json().get("id") or "")}


@tool(name="confluence_update_reply")
async def confluence_update_reply(
    comment_id: str, blocks: list[Any], run_id: str
) -> dict[str, Any]:
    """Edit an earlier reply — the "working on it" note becomes the answer."""
    if settings.read_only() or not comment_id:
        return {"updated": False}
    body = commands.blocks_to_storage([(str(b[0]), b[1]) for b in blocks])
    try:
        async with _wiki() as wiki:
            current = await wiki.get(
                f"/rest/api/content/{comment_id}", params={"expand": "version"}
            )
            current.raise_for_status()
            number = int(current.json()["version"]["number"]) + 1
            resp = await wiki.put(
                f"/rest/api/content/{comment_id}",
                json={
                    "type": "comment",
                    "version": {"number": number},
                    "body": {"storage": {"value": body, "representation": "storage"}},
                },
            )
            resp.raise_for_status()
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning(f"run {run_id}: could not update reply {comment_id}: {exc}")
        return {"updated": False, "error": str(exc)}
    return {"updated": True, "comment_id": comment_id}


@tool(name="save_code_plan")
async def save_code_plan(page_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    """The branches proposed (or confirmed) for coding, kept on the page."""
    try:
        async with _wiki() as wiki:
            await _set_page_property(wiki, page_id, settings.CODE_PLAN_PROPERTY, dict(plan))
    except (httpx.HTTPError, ValueError) as exc:
        return {"saved": False, "error": str(exc)}
    return {"saved": True}


# --- GitHub: the repository's default branch -----------------------------------------


@tool(name="branch_defaults")
async def branch_defaults(issue_key: str, summary: str) -> dict[str, Any]:
    """The proposal a person confirms: ``feature/<KEY>-<summary>`` as the working
    branch, and each repository's own branch as its source and target (2026-10-15):
    the one configured ("payments: org/payments@develop"), else the repository's
    default on GitHub. The first repository's is the plan's; a repository whose
    branch differs is named in ``per_repo``."""
    entries = gh.repo_entries(await org_repos.planning_repos())
    defaults: dict[str, str] = {}
    for name, slug, configured in entries:
        defaults[name] = configured or await _default_branch(slug) or "main"
    first = defaults[entries[0][0]] if entries else "main"
    plan = commands.BranchPlan(
        source=first,
        branch=commands.branch_slug(issue_key, summary),
        target=first,
        per_repo={n: {"source": b, "target": b} for n, b in defaults.items() if b != first},
    )
    return {
        "repo": entries[0][1] if entries else "",
        "repos": [slug for _, slug, _ in entries],
        "repo_names": {name: slug for name, slug, _ in entries},
        **plan.to_dict(),
    }


async def _default_branch(repo: str) -> str:
    """A repository's default branch on GitHub, or "" when it cannot be read."""
    if not settings.github_token():
        return ""
    try:
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=_TIMEOUT,
            headers={
                "Authorization": f"Bearer {settings.github_token()}",
                "Accept": "application/vnd.github+json",
            },
        ) as github:
            resp = await github.get(f"/repos/{repo}")
            resp.raise_for_status()
            return str(resp.json().get("default_branch") or "")
    except httpx.HTTPError as exc:
        logger.warning(f"Could not read the default branch of {repo}: {exc}")
        return ""


# --- after a merge ------------------------------------------------------------------


@tool(name="announce_merge")
async def announce_merge(issue_key: str, pr_number: int, pr_url: str, repo: str) -> dict[str, Any]:
    """Tell the story — and its TDD page — that its pull request was merged and
    Planning's code graph now has the merged code."""
    blocks = [
        ("✅ Pull request merged", ""),
        (
            "",
            f"[{repo}#{pr_number}]({pr_url}) was merged. The Planning agent refreshed its "
            "code graph with the merged code, so the next design starts from it.",
        ),
        ("", AGENT_FOOTER),
    ]
    out: dict[str, Any] = {"jira": False, "confluence": False}
    if settings.read_only() or not issue_key:
        return out
    posted = await story_comment(issue_key, blocks, "merge")
    out["jira"] = bool(posted.get("posted"))
    try:
        async with _client() as jira:
            tdd = await _issue_property(jira, issue_key, settings.TDD_PROPERTY)
    except (httpx.HTTPError, ValueError):
        tdd = {}
    if tdd.get("page_id"):
        replied = await confluence_reply(str(tdd["page_id"]), "", blocks, "merge")
        out["confluence"] = bool(replied.get("posted"))
    return out
