"""The ticket asked on becomes the root of its own hierarchy.

``@Aetherion build`` on a ticket that holds the requirement used to leave that
ticket as a bare trigger and create a new Epic that repeated it, word for word.
Now the ticket itself is the work item (owner decision, 2026-09-28): its type
follows its description, the children go under it, and no Epic is created
beside it.

=================================  ===========  ==========================
The description                    Root type    Children
=================================  ===========  ==========================
Medium / Large                     Epic         Stories, Sub-tasks under each
One capability                     Story        Sub-tasks
One technical job, no user value   Task         Sub-tasks, only for 2+ steps
Something broken in production     Bug          none — High / Highest
=================================  ===========  ==========================

An Epic stays an Epic: turning it into a Story would strand the Stories it
already holds. Its key, comments, attachments, links, reporter and history are
never touched; the description the person wrote is kept, verbatim, under
"Original request" at the end of the new one.

Checked live on BGV (team-managed) and FL (company-managed), 2026-09-28: a
plain edit of ``issuetype`` changes Task/Story to Epic/Story/Task/Bug — even
though ``editmeta`` does not list Epic — and sets Priority on both. A Sub-task
cannot be changed (400), and a Story that already has Sub-tasks *can* be made
an Epic, which leaves Sub-tasks under an Epic; both are refused here instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx
from common_lib.utils.logger import setup_logger

from src.jira.adf import adf, epic_description, story_description
from src.models.schemas import (
    Defect,
    Story,
    WorkBreakdown,
    WorkKind,
)
from src.shared.adf import heading, paragraph

logger = setup_logger(__name__)

# The heading the person's own text is kept under. Found again on a later run,
# so rebuilding a ticket never nests one "Original request" inside another.
ORIGINAL_HEADING = "Original request"

EPIC, STORY, TASK, BUG = "epic", "story", "task", "bug"

# A Bug needs BOTH: something failing, and it failing for real users. The model
# proposes "defect"; these only veto, so "add a CSV export" can never be filed
# as a production Bug because the model misread it.
_FAILING = re.compile(
    r"""\b(?:cannot|can't|can\s+not|unable\s+to|not\s+able\s+to|fail(?:s|ed|ing|ure)?|
        errors?|broken|crash(?:es|ed|ing)?|down|outage|500|502|503|timed?\s*out|
        not\s+(?:loading|working|showing|visible|opening|appearing|sent|received)|
        blank\s+page|data\s+loss|lost\s+data|missing\s+data)\b""",
    re.IGNORECASE | re.VERBOSE,
)
_LIVE = re.compile(
    r"""\b(?:production|prod|live|outage|customers?|clients?|
        all\s+(?:users|clients|customers|candidates|recruiters)|
        since\s+(?:the\s+)?(?:release|deploy(?:ment)?|update|upgrade|this\s+morning|yesterday|today))\b""",
    re.IGNORECASE | re.VERBOSE,
)
# Everyone, or a whole feature, affected — Highest rather than High.
_WIDESPREAD = re.compile(
    r"""\b(?:all\s+(?:users|clients|customers|candidates|recruiters)|everyone|every\s+user|
        nobody|no\s+one|no\s+users?|outage|down|data\s+loss|lost\s+data|whole|entire|500)\b""",
    re.IGNORECASE | re.VERBOSE,
)


def root_basis(root_key: str) -> str:
    """What a root's idempotency label is made from: its key, for life.

    Its description is rewritten on the first build, so a label hashed from
    the request would change with it and the next run would find nothing.
    """
    return f"root:{root_key.strip().upper()}"


def is_production_defect(text: str) -> bool:
    """True when the text reports something failing for real users."""
    return bool(_FAILING.search(text or "") and _LIVE.search(text or ""))


@dataclass(frozen=True)
class RootPlan:
    """What the root becomes and what goes under it."""

    role: str  # EPIC | STORY | TASK | BUG
    priority: str = ""  # Jira priority name; only a Bug sets one
    reason: str = ""  # why this type, in one clause, for the reply
    create_stories: bool = False
    create_subtasks: bool = False


def plan_root(breakdown: WorkBreakdown, current_type: str, request: str) -> RootPlan:
    """The root's new type, from the breakdown and the stated request.

    ``request`` is the stated request, not the whole bundle: the bundle carries
    every comment and history entry, so any "production" said anywhere on the
    ticket would count as evidence.
    """
    if (current_type or "").strip().lower() == EPIC:
        # An Epic holds Stories already, or may; a Small request adds one.
        return RootPlan(EPIC, reason="it is already an Epic", create_stories=True)

    if breakdown.epic is not None:
        return RootPlan(
            EPIC,
            reason=f"the requirement is {breakdown.classification.value} — several capabilities",
            create_stories=True,
        )

    kind = breakdown.work_kind
    if kind is WorkKind.DEFECT and not is_production_defect(request):
        logger.info("The model called this a defect; the request names no production failure.")
        kind = WorkKind.FEATURE

    if kind is WorkKind.DEFECT:
        widespread = bool(
            (breakdown.defect and breakdown.defect.widespread) or _WIDESPREAD.search(request)
        )
        return RootPlan(
            BUG,
            priority="Highest" if widespread else "High",
            reason=(
                "it reports a production problem affecting everyone"
                if widespread
                else "it reports a production problem"
            ),
        )

    story = breakdown.stories[0]
    if kind is WorkKind.TECHNICAL:
        return RootPlan(
            TASK,
            reason="it is one technical job with no user-facing behaviour",
            create_subtasks=len(story.subtasks) >= 2,
        )
    return RootPlan(STORY, reason="it is one capability", create_subtasks=True)


def refusal(plan: RootPlan, root: dict[str, Any], available: dict[str, str]) -> str:
    """Why the root must not be changed, or "" when it may.

    Everything here is checked before the first write, so a refusal leaves the
    ticket exactly as it was.
    """
    key = root["key"]
    if root.get("is_subtask"):
        return (
            f"{key} is a Sub-task, and a Sub-task cannot hold other tickets. "
            f"Nothing was changed. Comment on its parent"
            f"{' ' + root['parent_key'] if root.get('parent_key') else ''} instead."
        )
    target = available.get(plan.role, "")
    if not target:
        return (
            f"This project has no {plan.role.title()} issue type, so {key} could not "
            f"become one. Nothing was changed."
        )
    subtasks = root.get("subtask_keys") or []
    if plan.role == EPIC and subtasks and root.get("type", "").lower() != EPIC:
        return (
            f"{key} should become an Epic, but it already has Sub-tasks "
            f"({', '.join(subtasks)}), and an Epic cannot hold Sub-tasks. Nothing was "
            f"changed. Move those Sub-tasks to a Story (or delete them) and ask again."
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


def _bug_blocks(story: Story, defect: Defect | None, plan: RootPlan) -> list[tuple[str, Any]]:
    return [
        ("What happens now", defect.actual if defect else story.description),
        ("What should happen", defect.expected if defect else ""),
        ("Impact", defect.impact if defect else ""),
        ("Priority", f"{plan.priority} — {plan.reason}"),
        ("Fixed when", story.acceptance_criteria),
        ("Open questions — not stated in the requirement", story.open_questions),
    ]


def original_section(description: Any, summary_before: str) -> list[dict[str, Any]]:
    """The person's own words, under the "Original request" heading.

    Kept as ADF, not text, so images, tables and mentions survive. When the
    ticket was converted before, its section is carried over exactly as it is —
    heading, first summary and all — so a second build never nests one inside
    another or replaces the first summary with the agent's.
    """
    nodes = list((description or {}).get("content") or []) if isinstance(description, dict) else []
    for index, node in enumerate(nodes):
        if node.get("type") != "heading":
            continue
        text = "".join(str(c.get("text") or "") for c in node.get("content") or [])
        if text.strip() == ORIGINAL_HEADING:
            return nodes[index:]
    return [
        heading(ORIGINAL_HEADING),
        paragraph(f"Summary before: {summary_before}"),
        *(nodes or [paragraph("(The description was empty.)")]),
    ]


def root_description(
    breakdown: WorkBreakdown,
    plan: RootPlan,
    original: Any,
    summary_before: str,
) -> dict[str, Any]:
    """The structured description, then the original under its own heading."""
    story = breakdown.stories[0]
    if plan.role == EPIC and breakdown.epic is not None:
        new = epic_description(
            breakdown.epic, breakdown.epic.related_user_stories(breakdown.stories)
        )
    elif plan.role == BUG:
        new = adf(_bug_blocks(story, breakdown.defect, plan))
    elif plan.role == EPIC:
        # An existing Epic with a Small request: its own words stay the Epic's.
        new = adf([("Added by this request", f"{story.title}: {story.description}")])
    else:
        new = story_description(story)

    return {**new, "content": [*new["content"], *original_section(original, summary_before)]}


def new_summary(breakdown: WorkBreakdown, plan: RootPlan, current: str) -> str:
    """The generated title, or the current one when nothing better exists."""
    if plan.role == EPIC:
        return breakdown.epic.jira_summary if breakdown.epic is not None else current
    return breakdown.stories[0].title


# --------------------------------------------------------------------------
# Reading and writing the root
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


async def can_edit(client: httpx.AsyncClient, project_key: str) -> bool:
    """Whether this account may edit issues in the project."""
    resp = await client.get(
        "/rest/api/3/mypermissions",
        params={"projectKey": project_key, "permissions": "EDIT_ISSUES"},
    )
    resp.raise_for_status()
    perms = resp.json().get("permissions") or {}
    return bool((perms.get("EDIT_ISSUES") or {}).get("havePermission"))


async def change_type(client: httpx.AsyncClient, key: str, type_name: str) -> None:
    """Change only the issue type. Raises on refusal, having changed nothing."""
    resp = await client.put(
        f"/rest/api/3/issue/{key}", json={"fields": {"issuetype": {"name": type_name}}}
    )
    resp.raise_for_status()


async def rewrite(
    client: httpx.AsyncClient,
    key: str,
    *,
    summary: str,
    description: dict[str, Any],
    labels: list[str],
    priority: str = "",
) -> list[str]:
    """Summary, description, labels and priority. Returns what Jira refused.

    A project without a Priority field on its screen refuses the whole edit
    when priority is in it, so priority is retried without rather than losing
    the new description over it.
    """
    fields: dict[str, Any] = {"summary": summary[:255], "description": description}
    if priority:
        fields["priority"] = {"name": priority}
    body = {"fields": fields, "update": {"labels": [{"add": label} for label in labels]}}
    resp = await client.put(f"/rest/api/3/issue/{key}", json=body)
    if resp.status_code < 400 or not priority:
        resp.raise_for_status()
        return []
    reason = f"HTTP {resp.status_code}: {resp.text[:300]}"
    logger.warning(f"{key}: the edit with priority {priority} was refused ({reason}); retrying")
    fields.pop("priority")
    resp = await client.put(f"/rest/api/3/issue/{key}", json=body)
    resp.raise_for_status()
    return [f"Priority {priority} could not be set: {reason}"]
