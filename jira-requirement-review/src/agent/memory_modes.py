"""The router's two calls on the card's memory, outside a review (2026-10-30).

* ``mode="memory"``: for a TDD — the questions with every answer given in the
  comments so far, and the ranked services. The router builds Planning's
  input from them.
* ``mode="history"``: one fact in the project's history — a TDD version, a
  pull request, a merge — so a later card's review finds it.

Pure, SDK-free: ``execute`` runs the tools in ``tools/card_memory_tool.py``.
Each returns {"status": "success", ...} or {"status": "error", "message"}.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

Execute = Callable[..., Awaitable[dict]]


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "message": message}


async def card_memory(payload: dict[str, Any], execute: Execute) -> dict[str, Any]:
    issue_key = str(payload.get("issue_key") or "").strip()
    if not issue_key:
        return _error("memory needs the issue_key.")
    found = await execute(
        "refresh_card_memory",
        issue_key,
        str(payload.get("title") or ""),
        str(payload.get("card_text") or ""),
        list(payload.get("comments") or []),
        str(payload.get("repos") or ""),
        start_to_close_timeout=timedelta(minutes=3),
    )
    if not found.get("ok"):
        return _error(str(found.get("error") or "the memory could not be read"))
    return {
        "status": "success",
        "mode": "memory",
        "services": list(found.get("services") or []),
        "questions": list(found.get("questions") or []),
    }


async def card_history(payload: dict[str, Any], execute: Execute) -> dict[str, Any]:
    issue_key = str(payload.get("issue_key") or "").strip()
    event = str(payload.get("event") or "").strip()
    if not issue_key or not event:
        return _error("history needs the issue_key and the event.")
    written = await execute(
        "write_card_history",
        issue_key,
        event,
        dict(payload.get("data") or {}),
        str(payload.get("event_id") or ""),
        start_to_close_timeout=timedelta(minutes=1),
    )
    if not written.get("ok"):
        return _error(str(written.get("error") or "the history was not written"))
    return {"status": "success", "mode": "history", "actor": written.get("actor")}


MODES = {"memory": card_memory, "history": card_history}
