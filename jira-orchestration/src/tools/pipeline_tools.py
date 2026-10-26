"""@tools for the steps after a build (2026-10-08): what Planning is given, and
the design it wrote put on the ticket.

The order of the steps lives in ``agent/pipeline_flow.py``; what Planning is
told, in ``routing/pipeline.py``. Here: Jira and storage reads and writes.
"""

from __future__ import annotations

import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger

from routing import memory, pipeline, settings
from shared.adf import comment_text, to_text
from shared.keywords import is_agent_comment, mentions_trigger
from tools.org_repos import with_repos
from tools.tools import _client

logger = setup_logger(__name__)

_TICKET_FIELDS = "summary,description,issuetype"


async def _ticket(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    resp = await client.get(f"/rest/api/3/issue/{key}", params={"fields": _TICKET_FIELDS})
    resp.raise_for_status()
    fields = resp.json().get("fields") or {}
    return {
        "key": key,
        "type": (fields.get("issuetype") or {}).get("name") or "",
        "summary": fields.get("summary") or "",
        "description": (to_text(fields.get("description")) if fields.get("description") else ""),
    }


@tool(name="planning_input")
async def planning_input(
    issue_key: str,
    ticket_keys: list[str],
    proposed: list[str] | None = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The text Planning designs from: the request, its tickets and the review.

    Every ticket is read fresh from Jira — the build result carries only keys
    and summaries, and Planning needs the descriptions. A ticket that cannot be
    read is named without its description, never dropped.
    """
    issue_key = issue_key.strip().upper()
    tickets: list[dict[str, Any]] = []
    unread: list[str] = []
    try:
        async with _client() as client:
            root = await _ticket(client, issue_key)
            discussion = await _discussion(client, issue_key)
            keys = [k for k in ticket_keys if k and k.upper() != issue_key]
            for key in keys[: pipeline.MAX_TICKETS]:
                try:
                    tickets.append(await _ticket(client, key))
                except httpx.HTTPError as exc:
                    unread.append(key)
                    logger.warning(f"Planning input: could not read {key}: {exc}")
                    tickets.append({"key": key})
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(f"Planning input: could not read {issue_key}: {exc}", exc_info=True)
        return {"issue_text": "", "error": f"Could not read {issue_key}: {exc}"}
    text = pipeline.planning_text(root, tickets, list(proposed or []), review, discussion)
    return {"issue_text": text, "tickets": len(tickets), "unread": unread}


async def _discussion(client: httpx.AsyncClient, issue_key: str) -> list[dict[str, str]]:
    """Every comment on the story, oldest first: people's comments and requests,
    and the agent's own replies (2026-10-26) — the design reads the conversation."""
    keyword = settings.trigger_keyword()
    try:
        resp = await client.get(f"/rest/api/3/issue/{issue_key}", params={"fields": "comment"})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(f"Planning input: could not read the comments of {issue_key}: {exc}")
        return []
    out = []
    for c in ((resp.json().get("fields") or {}).get("comment") or {}).get("comments") or []:
        text = comment_text(c.get("body"), keep=keyword)
        if not text.strip():
            continue
        author = str((c.get("author") or {}).get("displayName") or "Person")
        if is_agent_comment(text):
            out.append({"who": "agent", "author": "Agent", "text": text})
        elif mentions_trigger(text, keyword=keyword):
            command, rest = pipeline.request_content(text, keyword)
            words = f"asked: {command}" + (f" — {rest}" if rest else "") if command else rest
            out.append({"who": "person", "author": author, "text": words})
        else:
            out.append({"who": "person", "author": author, "text": text})
    return out


@tool(name="story_text")
async def story_text(issue_key: str, ticket_keys: list[str]) -> dict[str, Any]:
    """The story as a requirement, for Planning's memory (2026-10-26).

    Its summary and description, its sub-tasks, and the comments people wrote
    about it — never the agent's own replies or the comments asking the agent
    something, which say nothing about the requirement.
    """
    issue_key = issue_key.strip().upper()
    keyword = settings.trigger_keyword()
    try:
        async with _client() as client:
            resp = await client.get(
                f"/rest/api/3/issue/{issue_key}",
                params={"fields": "summary,description,issuetype,comment"},
            )
            resp.raise_for_status()
            fields = resp.json().get("fields") or {}
            root = {
                "key": issue_key,
                "summary": fields.get("summary") or "",
                "description": to_text(fields["description"]) if fields.get("description") else "",
            }
            tickets = []
            for key in [k for k in ticket_keys if k and k.upper() != issue_key][
                : pipeline.MAX_TICKETS
            ]:
                try:
                    tickets.append(await _ticket(client, key))
                except httpx.HTTPError as exc:
                    logger.warning(f"Story text: could not read {key}: {exc}")
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": f"Could not read {issue_key}: {exc}"}
    comments = []
    for c in (fields.get("comment") or {}).get("comments") or []:
        text = comment_text(c.get("body"), keep=keyword)
        if not text.strip() or is_agent_comment(text):
            continue
        if mentions_trigger(text, keyword=keyword):
            # A request counts only for the words it adds to the requirement.
            added = pipeline.requirement_words(text, keyword)
            if added:
                comments.append(added)
            continue
        comments.append(text)
    text = memory.story_text(root, tickets, comments)
    return {"ok": True, "text": text, "hash": memory.normalised_hash(text)}


@tool(name="attach_tdd")
async def attach_tdd(issue_key: str, file_key: str, run_id: str) -> dict[str, Any]:
    """Put Planning's design (a PDF in storage) on the ticket, so a person can open it.

    Best effort: the design was written either way, and Coding reads it from
    storage, not from Jira. Says why when it could not.
    """
    issue_key = issue_key.strip().upper()
    if settings.read_only():
        return {"attached": False, "read_only": True}
    bucket = settings.storage_bucket()
    if not bucket:
        return {"attached": False, "error": "no storage configured (TENANT_ID)"}
    try:
        from common_lib.storage.storage_client import RetrievalMode, storage

        storage.init_client()
        data = storage.retrieve(bucket, file_key, RetrievalMode.FULL_OBJECT)
    except Exception as exc:  # noqa: BLE001 — reported on the ticket, never raised
        logger.warning(f"run {run_id}: could not read the design {file_key}: {exc}")
        return {"attached": False, "error": f"could not read the design from storage: {exc}"}

    # Planning names it "<UTC time>_<slug>"; the time tells two designs apart.
    stamp = PurePosixPath(file_key).stem.split("_", 1)[0][:20]
    filename = f"{issue_key}-technical-design-{stamp}.pdf"
    base, email, token = settings.jira_credentials()
    try:
        # Its own client: an upload is multipart, and Jira wants this header.
        async with httpx.AsyncClient(base_url=base, auth=(email, token), timeout=60.0) as client:
            resp = await client.post(
                f"/rest/api/3/issue/{issue_key}/attachments",
                headers={"X-Atlassian-Token": "no-check", "Accept": "application/json"},
                files={"file": (filename, bytes(data), "application/pdf")},
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(f"run {run_id}: could not attach the design to {issue_key}: {exc}")
        return {"attached": False, "error": f"Jira refused the attachment: {exc}"}
    logger.info(f"run {run_id}: attached {filename} to {issue_key}")
    return {"attached": True, "filename": filename}


# --- a request from the orchestrator's own form (2026-10-08) ---------------------------


@tool(name="request_settings")
async def request_settings(project_key: str = "") -> dict[str, Any]:
    """What a form request runs with: its id, the project, the steps and the gate.

    Read here, in an activity, because a workflow may not read the environment.
    Nothing is read from or written to Jira.
    """
    project = (project_key or "").strip().upper() or settings.request_project()
    allowed = settings.allowed_project_keys()
    problem = ""
    if not project:
        problem = "No Jira project to check for existing work: enter a Project key."
    elif allowed and project not in allowed:
        problem = f"{project} is not one of this agent's projects ({', '.join(allowed)})."
    return {
        "run_id": uuid.uuid4().hex[:8],
        "project_key": project,
        "problem": problem,
        "pipeline": await with_repos(settings.pipeline()),
        "task_queue_overrides": settings.task_queue_overrides(),
        "min_readiness": settings.request_min_readiness(),
        "webhooks": settings.webhook_status(),
    }


@tool(name="keep_design_copy")
async def keep_design_copy(file_key: str, label: str, run_id: str) -> dict[str, Any]:
    """A laptop run: a copy of a design (or any document) in docs-export, to open.

    On Aetherion nothing is written: the result's download link is the copy.
    """
    folder = settings.local_document_dir()
    if not (settings.local_run() and folder):
        return {"saved": False}
    bucket = settings.storage_bucket()
    try:
        from common_lib.storage.storage_client import RetrievalMode, storage

        storage.init_client()
        data = storage.retrieve(bucket, file_key, RetrievalMode.FULL_OBJECT)
        target = Path(folder).expanduser() / f"{label}-{PurePosixPath(file_key).name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(data))
    except Exception as exc:  # noqa: BLE001 — the design is still in storage
        logger.warning(f"run {run_id}: no local copy of {file_key}: {exc}")
        return {"saved": False, "error": str(exc)}
    logger.info(f"run {run_id}: saved {target}")
    return {"saved": True, "path": str(target.resolve())}
