"""@tools for the steps after a build (2026-10-08): what Planning is given, and
the design it wrote put on the ticket.

The order of the steps lives in ``agent/pipeline_flow.py``; what Planning is
told, in ``routing/pipeline.py``. Here: Jira and storage reads and writes.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger

from routing import memory, pipeline, settings
from shared import attachments
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
    """The text Planning designs from: the request, its tickets, the review, the
    discussion and every file attached to the story (2026-10-27).

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
            files = await _attachments(client, issue_key, root["summary"], media=True)
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
    text = pipeline.planning_text(root, tickets, list(proposed or []), review, discussion, files)
    return {
        "issue_text": text,
        "tickets": len(tickets),
        "unread": unread,
        "files": pipeline.files_read(files),
    }


async def _discussion(client: httpx.AsyncClient, issue_key: str) -> list[dict[str, str]]:
    """Every comment on the story, oldest first: people's comments and requests,
    and the agent's own replies (2026-10-26) — the design reads the conversation."""
    try:
        resp = await client.get(f"/rest/api/3/issue/{issue_key}", params={"fields": "comment"})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(f"Planning input: could not read the comments of {issue_key}: {exc}")
        return []
    return _discussion_of((resp.json().get("fields") or {}).get("comment"))


def _discussion_of(comment_field: dict[str, Any] | None) -> list[dict[str, str]]:
    """The conversation as Planning reads it, from Jira's ``comment`` field."""
    keyword = settings.trigger_keyword()
    out = []
    for c in (comment_field or {}).get("comments") or []:
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


async def _attachments(
    client: httpx.AsyncClient,
    issue_key: str,
    summary: str,
    media: bool,
    listed: list[dict[str, Any]] | None = None,
    keep_links: bool = False,
) -> list[dict[str, Any]]:
    """Every file attached to the story, read (2026-10-27): documents, sheets,
    text, and — with ``media`` — images, videos and recordings, which a model
    looks at. Oldest first; at most ``pipeline.MAX_FILES``. Each is
    ``{"id", "filename", "kind", "text", "note"}``: a file that could not be
    read has no text and says why. Never raises.

    ``listed``: Jira's attachment field, when the caller already has it.
    ``keep_links``: keep each file's download link, so its images and videos
    can be read later (``read_media``) without listing the files again."""
    if listed is None:
        try:
            resp = await client.get(
                f"/rest/api/3/issue/{issue_key}", params={"fields": "attachment"}
            )
            resp.raise_for_status()
            listed = (resp.json().get("fields") or {}).get("attachment") or []
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(f"Planning input: could not list the files on {issue_key}: {exc}")
            return []
    listed = sorted(listed, key=lambda a: (str(a.get("created") or ""), str(a.get("id") or "")))
    files = [
        {
            "id": str(a.get("id") or ""),
            "filename": str(a.get("filename") or "(unnamed file)"),
            "kind": attachments.media_kind(str(a.get("filename") or "")) or "document",
            "text": "",
            "note": "",
            "url": str(a.get("content") or ""),
            "mime": str(a.get("mimeType") or ""),
        }
        for a in listed
    ]
    for extra in files[pipeline.MAX_FILES :]:
        extra["note"] = f"not read: only the first {pipeline.MAX_FILES} files are read"
    await _read_files(client, issue_key, summary, files[: pipeline.MAX_FILES], media)
    if not keep_links:
        for file in files:
            file.pop("url"), file.pop("mime")
    return files


async def _read_files(
    client: httpx.AsyncClient,
    issue_key: str,
    summary: str,
    files: list[dict[str, Any]],
    media: bool,
) -> None:
    """Reads ``files`` in place: documents always, images and videos with
    ``media``. A file already read is not read again."""
    limits = attachments.default_limits()
    context = f"{issue_key}: {summary}".strip(": ")
    gate = asyncio.Semaphore(pipeline.FILES_AT_ONCE)

    async def read(file: dict[str, Any]) -> None:
        if file["kind"] != "document" and not media:
            file["note"] = f"{file['kind']}: compared by file, not by content"
            return
        if not file.get("url"):
            file["note"] = "no download link"
            return
        async with gate:
            try:
                got = await client.get(
                    file["url"], headers={"Accept": "*/*"}, follow_redirects=True
                )
                got.raise_for_status()
            except httpx.HTTPError as exc:
                file["note"] = f"could not be downloaded: {exc}"
                return
            found = await attachments.extract(
                got.content, file["filename"], file.get("mime", ""), limits=limits, context=context
            )
        file["text"], file["note"] = found.text, found.note

    todo = [f for f in files if not f.get("text")]
    for file in todo:
        file["note"] = ""
    tasks = [asyncio.create_task(read(f)) for f in todo]
    if tasks:
        _, late = await asyncio.wait(tasks, timeout=pipeline.FILES_SECONDS)
        for task in late:
            task.cancel()
    for file in todo:
        if not file["text"] and not file["note"]:
            file["note"] = f"not read within {pipeline.FILES_SECONDS // 60} minutes"


_BUNDLE_FIELDS = "summary,description,issuetype,comment,attachment"


@tool(name="story_bundle")
async def story_bundle(issue_key: str, ticket_keys: list[str]) -> dict[str, Any]:
    """The story read ONCE for a whole command (2026-10-29): summary,
    description, its sub-tasks, every comment, and its attached documents read.

    One issue read replaces the separate reads of ``story_text`` and
    ``planning_input``. Images and videos are listed, not looked at — the
    memory compares them by file — and keep their download link, so
    ``read_media`` reads them only when a design is really written."""
    issue_key = issue_key.strip().upper()
    keyword = settings.trigger_keyword()
    try:
        async with _client() as client:
            resp = await client.get(
                f"/rest/api/3/issue/{issue_key}", params={"fields": _BUNDLE_FIELDS}
            )
            resp.raise_for_status()
            fields = resp.json().get("fields") or {}
            root = {
                "key": issue_key,
                "type": (fields.get("issuetype") or {}).get("name") or "",
                "summary": fields.get("summary") or "",
                "description": to_text(fields["description"]) if fields.get("description") else "",
            }
            tickets, unread = [], []
            for key in [k for k in ticket_keys if k and k.upper() != issue_key][
                : pipeline.MAX_TICKETS
            ]:
                try:
                    tickets.append(await _ticket(client, key))
                except httpx.HTTPError as exc:
                    unread.append(key)
                    logger.warning(f"Story bundle: could not read {key}: {exc}")
                    tickets.append({"key": key})
            files = await _attachments(
                client,
                issue_key,
                root["summary"],
                media=False,
                listed=list(fields.get("attachment") or []),
                keep_links=True,
            )
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(f"Story bundle: could not read {issue_key}: {exc}", exc_info=True)
        return {"ok": False, "error": f"Could not read {issue_key}: {exc}"}
    memory_comments, people = [], []
    for c in (fields.get("comment") or {}).get("comments") or []:
        text = comment_text(c.get("body"), keep=keyword)
        if not text.strip() or is_agent_comment(text):
            continue
        if mentions_trigger(text, keyword=keyword):
            added = pipeline.requirement_words(text, keyword)
            if added:
                memory_comments.append(added)
                people.append({"id": str(c.get("id") or ""), "text": added})
            continue
        memory_comments.append(text)
        people.append({"id": str(c.get("id") or ""), "text": text})
    return {
        "ok": True,
        "root": root,
        "tickets": tickets,
        "unread": unread,
        "discussion": _discussion_of(fields.get("comment")),
        # For the memory: people's comments only, requests reduced to what they add.
        "comments": memory_comments,
        # The same, with their ids: what the review's questions are answered by.
        "people_comments": people,
        "files": files,
    }


@tool(name="read_media")
async def read_media(issue_key: str, summary: str, files: list[dict[str, Any]]) -> dict[str, Any]:
    """The story's images, videos and recordings read by a model (2026-10-29),
    from the links ``story_bundle`` kept; documents already read are left as
    they are. Returns the files without their links."""
    files = [dict(f) for f in files]
    media = [f for f in files[: pipeline.MAX_FILES] if f.get("kind") != "document"]
    if media:
        async with _client() as client:
            await _read_files(client, issue_key.strip().upper(), summary, media, media=True)
    for file in files:
        file.pop("url", None), file.pop("mime", None)
    return {"ok": True, "files": files}


@tool(name="story_text")
async def story_text(issue_key: str, ticket_keys: list[str], media: bool = False) -> dict[str, Any]:
    """The story as a requirement, for Planning's memory (2026-10-26).

    Its summary and description, its sub-tasks, the comments people wrote about
    it — never the agent's own replies or the comments asking the agent
    something, which say nothing about the requirement — and its attached files
    (2026-10-27). Documents are compared by their text; images and videos by
    the file, since a model's reading of them differs run to run. With
    ``media`` (a bug investigation) the images and videos are read too.
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
            files = await _attachments(client, issue_key, root["summary"], media)
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
    text = memory.story_text(root, tickets, comments, files)
    return {
        "ok": True,
        "text": text,
        "hash": memory.normalised_hash(text),
        "files": pipeline.files_read(files),
    }


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
