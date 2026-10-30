"""What the review remembers about a card, and what it is told before it
reviews (2026-10-30).

Before the review: the card's earlier questions with the answers people have
given since (``recall``), earlier cards about the same thing with their
services, branches and pull requests, and the services this card touches,
ranked. All of it is shown to the review as one more source, so it does not
ask what is already answered or settled by earlier work.

After the review: its new questions and the ranked services are kept, and the
card goes into the project's history (``keep``). For a TDD the same memory is
brought up to date with the comments since the review (``refresh``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from card_memory import history, questions, ranking

logger = logging.getLogger(__name__)

SOURCE_ID = "card_memory"
SOURCE_LABEL = "Earlier answers and related work"
MAX_EARLIER = 5


async def _related(card_key: str, title: str, card_text: str) -> list[dict[str, Any]]:
    try:
        return await asyncio.to_thread(history.find, card_key, [card_text, title])
    except Exception as error:  # noqa: BLE001 - ranked from the card alone
        logger.warning("No history for %s: %s", card_key, error)
        return []


async def recall(
    card_key: str, title: str, card_text: str, comments: list[dict], repos_text: str
) -> dict[str, Any]:
    """The card's memory before its review: {"seq", "snapshot", "services",
    "related"}. Nothing is saved yet; ``keep`` saves once, after the review."""
    seq, stored = await asyncio.to_thread(questions.load, card_key)
    snapshot = await questions.with_comments(card_key, stored, comments)
    related = await _related(card_key, title, card_text)
    services = await ranking.rank(card_text, repos_text, related)
    return {
        "seq": seq,
        "snapshot": snapshot,
        "changed": snapshot != stored,
        "services": services,
        "related": related,
    }


def document(memory: dict[str, Any]) -> dict[str, Any] | None:
    """The memory as one more source for the review; None when it holds nothing."""
    snapshot = memory.get("snapshot") or {}
    answered = [q for q in snapshot.get("questions") or [] if q.get("answers")]
    still_open = [q for q in snapshot.get("questions") or [] if not q.get("answers")]
    earlier = list(memory.get("related") or [])[:MAX_EARLIER]
    services = list(memory.get("services") or [])
    if not (answered or still_open or earlier or services):
        return None
    lines = [
        "What is already known about this card and earlier work in the project. Treat an "
        "answer below as part of the requirement and never ask that question again. Use the "
        "earlier cards to ask sharper questions (for example whether a rule an earlier card "
        "set also applies here); do not ask what they already settled unless this card says "
        "something different."
    ]
    if answered:
        lines += ["", "Questions already answered in the comments:"]
        for q in answered:
            said = " / ".join(a["text"] for a in q["answers"])
            lines.append(f"- {q['question']} Answer: {said}")
    if still_open:
        lines += ["", "Questions asked before and not answered yet:"]
        lines += [f"- {q['question']}" for q in still_open]
    if earlier:
        lines += ["", "Earlier cards about the same thing:"]
        for card in earlier:
            line = f"- {card['card']} {card.get('title') or ''}".rstrip()
            if card.get("services"):
                line += f": changed {', '.join(card['services'])}"
            prs = [
                f"PR #{p['pr']} on {p.get('branch') or 'its branch'} ({p.get('state', 'open')})"
                for p in card.get("prs") or []
            ]
            if prs:
                line += f"; {'; '.join(prs)}"
            lines.append(line)
    if services:
        lines += ["", "Services this card touches, ranked 0-10:"]
        lines += [f"- {s['service']} {s['rank']}/10: {s['reason']}" for s in services]
    return {
        "source_id": SOURCE_ID,
        "source_label": SOURCE_LABEL,
        "kind": "card_memory",
        "text": "\n".join(lines),
        "metadata": {"answered": len(answered), "earlier_cards": len(earlier)},
    }


def keep(
    card_key: str,
    memory: dict[str, Any],
    asked: list[str],
    title: str,
    summary: str,
) -> dict[str, Any]:
    """After the review: its questions and the ranked services saved, the card
    in the project's history. Returns {"services", "questions"}."""
    seq, before = memory.get("seq") or 0, memory.get("snapshot") or {}
    services = list(memory.get("services") or [])
    updated = {**questions.with_questions(before, asked), "services": services}
    if memory.get("changed") or updated != before:
        questions.save(card_key, seq + 1, updated)
    if services:
        history.write(
            card_key,
            "card",
            {
                "title": title,
                "services": [s["service"] for s in services],
                "summary": (summary or "")[:500],
            },
        )
    return {"services": services, "questions": questions.view(updated)}


async def refresh(
    card_key: str, title: str, card_text: str, comments: list[dict], repos_text: str
) -> dict[str, Any]:
    """For a TDD: the questions with every answer given so far, and the ranked
    services (ranked now when the card was never reviewed with its memory).
    Returns {"services", "questions"}."""
    seq, snapshot = await asyncio.to_thread(questions.load, card_key)
    updated = await questions.with_comments(card_key, snapshot, comments)
    services = list(updated.get("services") or [])
    if not services:
        related = await _related(card_key, title, card_text)
        services = await ranking.rank(card_text, repos_text, related)
        updated = {**updated, "services": services}
    if updated != snapshot:
        await asyncio.to_thread(questions.save, card_key, seq + 1, updated)
    return {"services": services, "questions": questions.view(updated)}
