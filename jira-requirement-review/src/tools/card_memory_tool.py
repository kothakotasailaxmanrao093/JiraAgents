"""@tools: the review's memory of a card, in Memsy (2026-10-30).

* ``recall_card_memory``: before a review — earlier questions with the answers
  given since, earlier cards about the same thing, the ranked services, and
  the source the review reads all of it from.
* ``keep_card_memory``: after a review — its questions and services saved, the
  card in the project's history.
* ``refresh_card_memory``: for a TDD — the questions with every answer so far
  and the ranked services, for Planning's input.
* ``write_card_history``: one fact in the project's history (a TDD version, a
  pull request, a merge).

Memsy not answering never fails a review: the tool reports it, and the review
goes on without its memory.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aetherion_sdk import tool

from card_memory import card, history

logger = logging.getLogger(__name__)


@tool(name="recall_card_memory")
async def recall_card_memory(
    issue_key: str,
    title: str,
    card_text: str,
    comments: list[dict[str, Any]],
    repos: str,
) -> dict[str, Any]:
    try:
        memory = await card.recall(issue_key, title, card_text, list(comments or []), repos)
    except Exception as error:  # noqa: BLE001 - the review runs without its memory
        logger.warning("No memory for %s: %s", issue_key, error)
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return {"ok": True, "memory": memory, "document": card.document(memory)}


@tool(name="keep_card_memory")
async def keep_card_memory(
    issue_key: str,
    memory: dict[str, Any],
    asked: list[str],
    title: str,
    summary: str,
) -> dict[str, Any]:
    try:
        kept = await asyncio.to_thread(
            card.keep, issue_key, dict(memory or {}), list(asked or []), title, summary
        )
    except Exception as error:  # noqa: BLE001 - the review's reply stands without it
        logger.warning("Memory of %s not kept: %s", issue_key, error)
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return {"ok": True, **kept}


@tool(name="refresh_card_memory")
async def refresh_card_memory(
    issue_key: str,
    title: str,
    card_text: str,
    comments: list[dict[str, Any]],
    repos: str,
) -> dict[str, Any]:
    try:
        kept = await card.refresh(issue_key, title, card_text, list(comments or []), repos)
    except Exception as error:  # noqa: BLE001 - reported; the TDD uses its old input
        logger.warning("Memory of %s not refreshed: %s", issue_key, error)
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return {"ok": True, **kept}


@tool(name="write_card_history")
async def write_card_history(
    issue_key: str, event: str, data: dict[str, Any], event_id: str = ""
) -> dict[str, Any]:
    try:
        await asyncio.to_thread(history.write, issue_key, event, dict(data or {}), event_id)
    except Exception as error:  # noqa: BLE001 - reported to the caller
        logger.warning("History of %s not written: %s", issue_key, error)
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return {"ok": True, "actor": history.actor_of(issue_key)}
