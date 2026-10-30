"""The card's memory, kept by Orchestration (2026-10-30).

Orchestration is the one that remembers. Before a review it recalls what is
known about the card (answers given since, earlier cards with their PRs and
branches, decisions in force, the services ranked) and gives it to the review
as one more source; after the review it keeps the review's questions and the
card in the project's history. For a TDD it brings the memory up to date with
the comments since and gives Planning the agreed input. Pull requests and
merges go into the history.

Every step is best effort: the memory not answering never stops a review, a
TDD or a merge — the request then runs as before. SDK-free: the caller passes
``run`` (one memory tool; its result as a dict, never raising).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from routing import handoff

logger = logging.getLogger(__name__)

# run(tool name, *args) -> the tool's result; {"ok": False, ...} on any failure.
Run = Callable[..., Awaitable[dict[str, Any]]]


def _card_text(bundle: dict[str, Any]) -> tuple[str, str]:
    card = handoff.card(bundle)
    return card["title"], handoff.card_text(card)


async def recall(
    run: Run, issue_key: str, bundle: dict[str, Any], repos: str
) -> dict[str, Any] | None:
    """{"memory", "document"} before a review; None when the memory cannot say."""
    title, text = _card_text(bundle)
    out = await run(
        "recall_card",
        issue_key,
        title,
        text,
        list(bundle.get("people_comments") or []),
        repos,
    )
    if not out.get("ok"):
        logger.warning(f"{issue_key}: no memory before the review: {out.get('error')}")
        return None
    return {"memory": out["memory"], "document": out.get("document")}


async def keep(
    run: Run,
    issue_key: str,
    recalled: dict[str, Any],
    asked: list[str],
    title: str,
    summary: str,
) -> dict[str, Any] | None:
    """{"services", "questions"} after a review; None when not kept."""
    out = await run("keep_card", issue_key, recalled["memory"], asked, title, summary)
    if not out.get("ok"):
        logger.warning(f"{issue_key}: the review's memory was not kept: {out.get('error')}")
        return None
    return {"services": list(out["services"]), "questions": list(out["questions"])}


async def refresh(
    run: Run, issue_key: str, bundle: dict[str, Any], repos: str
) -> dict[str, Any] | None:
    """{"services", "questions"} for a TDD, up to date with the comments; None
    when the memory cannot say."""
    title, text = _card_text(bundle)
    out = await run(
        "refresh_card",
        issue_key,
        title,
        text,
        list(bundle.get("people_comments") or []),
        repos,
    )
    if not out.get("ok"):
        logger.warning(f"{issue_key}: no memory for the TDD: {out.get('error')}")
        return None
    return {"services": list(out["services"]), "questions": list(out["questions"])}


async def remember(
    run: Run, issue_key: str, event: str, data: dict[str, Any], event_id: str = ""
) -> None:
    """One fact in the project's history; a miss is logged, never raised."""
    out = await run("write_history", issue_key, event, data, event_id)
    if not out.get("ok"):
        logger.warning(f"{issue_key}: the {event} history was not kept: {out.get('error')}")
