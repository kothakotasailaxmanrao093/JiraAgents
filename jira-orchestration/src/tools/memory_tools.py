"""@tools: the agents' memory, kept by Orchestration alone (2026-10-30).

Orchestration reads the memory before it calls an agent, gives the agent what
it needs, and keeps what the agent hands back. Requirement Review and
Planning store nothing themselves.

Cards (memory_service/cards.py, history.py):
  recall_card      before a review: answers since, earlier cards, ranked
                   services, decisions in force — and the source the review reads
  keep_card        after a review: its questions and services; the card's history
  refresh_card     for a TDD: every answer so far and the ranked services
  write_history    a TDD version, a pull request, a merge

TDDs (memory_service/designs.py):
  tdd_memory       the story's TDD snapshot, for Planning's facts check and record
  keep_tdd_memory  the snapshot Planning returns after a TDD is published
  design_context   facts, decisions and earlier conversation, for a question
  remember_exchange  the question and Planning's answer
  decisions_in_force the decisions a new design must keep

Memsy not answering never fails a request: each tool reports {"ok": False,
"error"} and the caller goes on without the memory.
"""

from __future__ import annotations

import logging
from typing import Any

from aetherion_sdk import tool

from memory_service import cards, designs, history, ranking

logger = logging.getLogger(__name__)


def _failed(what: str, key: str, error: Exception) -> dict[str, Any]:
    logger.warning("%s for %s failed: %s", what, key, error)
    return {"ok": False, "error": f"{type(error).__name__}: {error}"}


@tool(name="recall_card")
async def recall_card(
    issue_key: str, title: str, card_text: str, comments: list[dict], repos: str
) -> dict[str, Any]:
    try:
        memory = await cards.recall(issue_key, title, card_text, list(comments or []), repos)
    except Exception as error:  # noqa: BLE001 - the review runs without its memory
        return _failed("Recalling the card", issue_key, error)
    return {"ok": True, "memory": memory, "document": cards.document(memory)}


@tool(name="keep_card")
async def keep_card(
    issue_key: str, memory: dict, asked: list[str], title: str, summary: str
) -> dict[str, Any]:
    try:
        kept = await cards.keep(issue_key, dict(memory or {}), list(asked or []), title, summary)
    except Exception as error:  # noqa: BLE001 - the review's reply stands without it
        return _failed("Keeping the card", issue_key, error)
    return {"ok": True, **kept}


@tool(name="refresh_card")
async def refresh_card(
    issue_key: str, title: str, card_text: str, comments: list[dict], repos: str
) -> dict[str, Any]:
    try:
        kept = await cards.refresh(issue_key, title, card_text, list(comments or []), repos)
    except Exception as error:  # noqa: BLE001 - ranked without the memory instead
        _failed("Refreshing the card", issue_key, error)
        return await rank_card(card_text, repos)
    return {"ok": True, **kept}


@tool(name="rank_card")
async def rank_card(card_text: str, repos: str) -> dict[str, Any]:
    """The services a card touches, ranked with no memory (2026-11-03): one model
    call over the card and the repositories. Planning needs a card with ranked
    services even when Memsy cannot be read; the questions are then none."""
    try:
        services = await ranking.rank(card_text, repos, [])
    except Exception as error:  # noqa: BLE001 - reported to the caller
        return _failed("Ranking the card", "", error)
    return {"ok": True, "services": services, "questions": [], "memory": False}


@tool(name="write_history")
async def write_history(
    issue_key: str, event: str, data: dict, event_id: str = ""
) -> dict[str, Any]:
    try:
        await history.write(issue_key, event, dict(data or {}), event_id)
    except Exception as error:  # noqa: BLE001 - reported to the caller
        return _failed(f"The {event} history", issue_key, error)
    return {"ok": True, "actor": history.actor_of(issue_key)}


@tool(name="tdd_memory")
async def tdd_memory(issue_key: str) -> dict[str, Any]:
    try:
        seq, snapshot = await designs.load(issue_key)
    except Exception as error:  # noqa: BLE001 - the TDD is written as before
        return _failed("Reading the TDD memory", issue_key, error)
    return {"ok": True, "seq": seq, "snapshot": snapshot}


@tool(name="keep_tdd_memory")
async def keep_tdd_memory(
    issue_key: str,
    seq: int,
    snapshot: dict,
    version: str,
    page_id: str,
    decisions: list[dict],
    superseded: list[str],
) -> dict[str, Any]:
    try:
        await designs.save(issue_key, int(seq), dict(snapshot or {}))
        await designs.remember_version(
            issue_key,
            version,
            page_id,
            list(decisions or []),
            list(superseded or []),
        )
    except Exception as error:  # noqa: BLE001 - the TDD is published either way
        return _failed("Keeping the TDD memory", issue_key, error)
    return {"ok": True}


@tool(name="design_context")
async def design_context(issue_key: str, question: str) -> dict[str, Any]:
    try:
        _, snapshot = await designs.load(issue_key)
        decisions = await designs.decisions_in_force(issue_key)
        earlier = await designs.recall(issue_key, question)
    except Exception as error:  # noqa: BLE001 - Planning answers from the page alone
        return _failed("The design's memory", issue_key, error)
    return {
        "ok": True,
        "facts": list(snapshot.get("facts") or []),
        "decisions": decisions,
        "earlier": earlier,
    }


@tool(name="remember_exchange")
async def remember_exchange(
    issue_key: str, page_id: str, question: str, answer: str, kind: str, verdict: str
) -> dict[str, Any]:
    try:
        await designs.remember_exchange(issue_key, page_id, question, answer, kind, verdict)
    except Exception as error:  # noqa: BLE001 - the answer is posted either way
        return _failed("Remembering the exchange", issue_key, error)
    return {"ok": True}


@tool(name="decisions_in_force")
async def decisions_in_force(issue_key: str, services: list[str]) -> dict[str, Any]:
    try:
        found = await designs.decisions_in_force(issue_key, list(services or []))
    except Exception as error:  # noqa: BLE001 - designed without them
        return _failed("The decisions in force", issue_key, error)
    return {"ok": True, "decisions": found}
