"""Work built on the ticket asked on — which is kept exactly as it is.

Owner decision, 2026-10-07 (replacing the 2026-09-28 design that re-typed the
ticket from its description and rewrote its summary and description): the
person chose the card's type, so the card's type decides what goes under it,
and its type, summary and description are never changed.

===========  =======================================
The card     What is built under it
===========  =======================================
Epic         Stories, each with its Sub-tasks
Story        Sub-tasks
Task / Bug   Sub-tasks, only for two or more steps
Sub-task     nothing — a Sub-task cannot hold tickets
===========  =======================================

No Epic is ever created beside the card. Its key, comments, attachments, links,
reporter and history are never touched either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from common_lib.utils.logger import setup_logger

from src.models.schemas import WorkBreakdown

logger = setup_logger(__name__)

EPIC, STORY, TASK, BUG = "epic", "story", "task", "bug"


def root_basis(root_key: str) -> str:
    """What a root's idempotency label is made from: its key, for life.

    Hashed from the key rather than the request, so asking again — even after
    the card's text was edited by a person — finds the tickets already built.
    """
    return f"root:{root_key.strip().upper()}"


@dataclass(frozen=True)
class RootPlan:
    """What goes under the card (its type itself never changes)."""

    role: str  # the card's own type, lower case: epic, story, task, bug …
    reason: str = ""  # "it is a Story", for the reply
    create_stories: bool = False
    create_subtasks: bool = False


def plan_root(breakdown: WorkBreakdown, current_type: str) -> RootPlan:
    """What goes under the card — decided by the card's own type (2026-10-07).

    The person chose the type, so it is kept: an Epic gets Stories, a Story
    gets Sub-tasks, a Task or Bug (or any other type) gets Sub-tasks only when
    the work has two or more steps. The description no longer re-types the
    card, and the card is never edited (see :func:`refusal` and the tool).
    """
    kind = (current_type or "").strip().lower()
    steps = sum(len(story.subtasks) for story in breakdown.stories)
    if kind == EPIC:
        return RootPlan(EPIC, reason="it is an Epic", create_stories=True)
    if kind == STORY:
        return RootPlan(STORY, reason="it is a Story", create_subtasks=steps > 0)
    role = kind if kind in (TASK, BUG) else (kind or TASK)
    return RootPlan(role, reason=f"it is a {current_type or 'Task'}", create_subtasks=steps >= 2)


def refusal(plan: RootPlan, root: dict[str, Any], available: dict[str, str]) -> str:
    """Why nothing may be built under the card, or "" when it may.

    Checked before the first write, so a refusal leaves everything as it was.
    The card's type is never changed, so only a Sub-task — which cannot hold
    other tickets — is refused here.
    """
    key = root["key"]
    if root.get("is_subtask"):
        return (
            f"{key} is a Sub-task, and a Sub-task cannot hold other tickets. "
            f"Nothing was changed. Comment on its parent"
            f"{' ' + root['parent_key'] if root.get('parent_key') else ''} instead."
        )
    return ""


def type_names(available: list[str], resolved: dict[str, str]) -> dict[str, str]:
    """Role -> this project's type name, for every role a root can take."""
    offered = {name.strip().lower(): name for name in available}
    return {
        EPIC: resolved.get("epic", "") if resolved.get("epic", "").lower() in offered else "",
        STORY: resolved.get("story", "") if resolved.get("story", "").lower() in offered else "",
        TASK: offered.get("task", ""),
        BUG: offered.get("bug", ""),
    }


# --------------------------------------------------------------------------
# The new description
# --------------------------------------------------------------------------


_ROOT_FIELDS = "summary,description,issuetype,subtasks,parent,labels,priority,project"


async def read_root(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    """The root as it is now — read fresh, right before it is changed."""
    resp = await client.get(f"/rest/api/3/issue/{key}", params={"fields": _ROOT_FIELDS})
    resp.raise_for_status()
    data = resp.json()
    fields = data.get("fields") or {}
    issuetype = fields.get("issuetype") or {}
    return {
        "key": data.get("key", key),
        "summary": fields.get("summary", "") or "",
        "description": fields.get("description"),
        "type": issuetype.get("name", "") or "",
        "is_subtask": bool(issuetype.get("subtask")),
        "parent_key": (fields.get("parent") or {}).get("key", "") or "",
        "subtask_keys": [s.get("key", "") for s in fields.get("subtasks") or [] if s.get("key")],
        "labels": [str(x) for x in fields.get("labels") or []],
    }
