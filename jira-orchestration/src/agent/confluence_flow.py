"""The design discussion on a TDD page: one check of its comments (2026-10-08).

Started every minute (Aetherion Schedule, or scripts/run_local.sh --poll):

    {"confluence_poll": true}

Each new "@Aetherion …" comment on a TDD page is answered under it:
"explain why …" / "why not …" go to the Planning agent, "update the TDD" revises
the page, "code" confirms the branches and then raises the pull request — linked
on the page and on its Jira story. A comment is recorded as answered before the
work starts, so a slow command is never started twice by the next check.

SDK-free like ``router_flow``: the executors are passed in.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from agent.command_flow import Command, Dispatch, Execute, reply, run_command
from routing.catalog import by_intent
from routing.classify import classify_workflow

logger = logging.getLogger(__name__)

_TIMEOUT = timedelta(minutes=2)
PAGE_HELP = [
    '"@Aetherion explain why this approach was selected"',
    '"@Aetherion why can\'t we use <alternative>?"',
    '"@Aetherion update the TDD" — apply what the discussion agreed',
    '"@Aetherion code" — implement it as a pull request, after confirming the branches',
]


async def run_confluence_poll(execute: Execute, dispatch: Dispatch) -> dict[str, Any]:
    """Answer every new command on the TDD pages; the result says what was done."""
    scan = await _safe(execute, "confluence_scan")
    if not scan.get("ok"):
        return {"status": "confluence_poll", "error": scan.get("error") or "scan failed"}
    done: list[dict[str, Any]] = []
    for event in scan.get("events") or []:
        done.append(await _answer(event, scan, execute, dispatch))
    return {"status": "confluence_poll", "pages": scan.get("pages", 0), "answered": done}


async def run_confluence_comment(
    execute: Execute, dispatch: Dispatch, page_id: str, comment_id: str
) -> dict[str, Any]:
    """One comment, pushed by the Confluence webhook as it was posted (2026-10-14) —
    no polling. Answered exactly as the poll answers it, under the comment."""
    found = await _safe(execute, "confluence_comment", page_id, comment_id)
    if not found.get("ok"):
        return {"status": "confluence_comment", "error": found.get("error") or "read failed"}
    done = [await _answer(event, found, execute, dispatch) for event in found.get("events") or []]
    return {
        "status": "confluence_comment",
        "comment": comment_id,
        "answered": done,
        "skipped": found.get("skipped") or "",
    }


async def _answer(
    event: dict[str, Any], scan: dict[str, Any], execute: Execute, dispatch: Dispatch
) -> dict[str, Any]:
    page_id, comment_id = str(event["page_id"]), str(event["id"])
    root = str(event.get("root_id") or comment_id)
    keyword = str(scan.get("keyword") or "Aetherion")
    run_id = f"cf{comment_id}"[-12:]
    # Recorded first: the next check must not start this again while it runs.
    await _safe(execute, "confluence_mark_answered", page_id, comment_id)

    decision = classify_workflow(str(event.get("text") or ""), keyword)
    if decision is None or decision.intent == "TDD":
        blocks = reply(
            "What I can do on a TDD page",
            ("Ask me", [line.replace("Aetherion", keyword) for line in PAGE_HELP]),
        )
        await _safe(execute, "confluence_reply", page_id, root, blocks, run_id)
        return {"comment": comment_id, "intent": "HELP"}

    posted = await _safe(
        execute,
        "confluence_reply",
        page_id,
        root,
        reply("⏳ Working on it…", ("What is happening", "Started.")),
        run_id,
    )
    reply_id = str(posted.get("comment_id") or "")

    async def say(blocks: list[tuple[str, object]]) -> None:
        if reply_id:
            await _safe(execute, "confluence_update_reply", reply_id, blocks, run_id)

    overrides = (scan.get("settings") or {}).get("task_queue_overrides") or {}
    review = by_intent("REVIEW")
    if review is not None:
        review = review.with_task_queue(overrides.get(review.agent_name))
    cmd = Command(
        intent=decision.intent,
        text=str(event.get("text") or ""),
        issue_key="",
        run_id=run_id,
        execute=execute,
        dispatch=dispatch,
        say=say,
        settings=scan.get("settings") or {},
        keyword=keyword,
        review_spec=review,
        page_id=page_id,
    )
    result = await run_command(cmd)
    if reply_id:
        updated = await _safe(execute, "confluence_update_reply", reply_id, result.blocks, run_id)
        if not updated.get("updated"):
            await _safe(execute, "confluence_reply", page_id, root, result.blocks, run_id)
    else:
        await _safe(execute, "confluence_reply", page_id, root, result.blocks, run_id)
    if result.links.get("pr") and cmd.issue_key:
        # The pull request is linked on the Jira story too.
        await _safe(execute, "story_comment", cmd.issue_key, result.blocks, run_id)
    logger.info(f"page {page_id}: answered {decision.intent} comment {comment_id} ({result.ran})")
    return {
        "comment": comment_id,
        "intent": decision.intent,
        "ran": result.ran,
        "links": result.links,
    }


async def _safe(execute: Execute, name: str, *args: Any) -> dict[str, Any]:
    try:
        out = await execute(name, *args, start_to_close_timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — the result says what failed
        logger.error(f"{name} raised: {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}
    return out if isinstance(out, dict) else {}
