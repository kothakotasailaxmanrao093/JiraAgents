"""The card's memory, kept by the Requirement Review agent (2026-10-30).

The review itself reads and keeps the memory (asked with ``memory_repos``).
Outside a review the router asks Review for it:

* ``memory``: for a TDD — the review's questions with every answer given in
  the comments so far, and the services the card touches, ranked 0-10.
* ``remember``: one fact for the project's history — a TDD version, a pull
  request, a merge — so a later card's review finds it.

Every step is best effort: Review or Memsy not answering never stops a TDD or
a merge — the TDD then gets the old input. SDK-free: the caller passes ``ask``
(one Review run; its output, or None).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

REVIEW_AGENT = "JiraRequirementReview"
TIMEOUT_MINUTES = 10

# ask(payload, name) -> Review's output when it succeeded, else None.
Ask = Callable[[dict[str, Any], str], Awaitable[dict[str, Any] | None]]


def output(raw: Any) -> dict[str, Any] | None:
    """Review's answer to a memory call, or None when it did not succeed."""
    out = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(out, dict) or out.get("status") != "success":
        return None
    return out


async def memory(
    ask: Ask,
    issue_key: str,
    title: str,
    card_text: str,
    comments: list[dict[str, str]],
    repos: str,
) -> dict[str, Any] | None:
    """{"questions": [...], "services": [...]} as Review keeps them now; None
    when Review cannot say."""
    out = await ask(
        {
            "mode": "memory",
            "issue_key": issue_key,
            "title": title,
            "card_text": card_text,
            "comments": comments,
            "repos": repos,
        },
        "memory",
    )
    if out is None:
        return None
    return {
        "questions": list(out.get("questions") or []),
        "services": list(out.get("services") or []),
    }


async def remember(
    ask: Ask, issue_key: str, event: str, data: dict[str, Any], event_id: str = ""
) -> None:
    """One fact in the project's history; a miss is logged, never raised."""
    out = await ask(
        {
            "mode": "history",
            "issue_key": issue_key,
            "event": event,
            "data": data,
            "event_id": event_id,
        },
        f"history-{event}",
    )
    if out is None:
        logger.warning(f"{issue_key}: the {event} history was not kept")
