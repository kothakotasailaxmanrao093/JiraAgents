"""A bug is investigated, never broken into work items (2026-10-22). Pure.

"@Aetherion build" on a Bug, or on a description of something broken, used to
create a Task with Sub-tasks, or Sub-tasks under the Bug. A bug needs its cause
found first; the Review agent's bug investigation does that, and the TDD and
code commands follow only when a person asks for them.
"""

from __future__ import annotations

BUG_TYPES = frozenset({"bug", "defect"})

BUG_HEADLINE = "Nothing created — a bug is investigated, not broken down"


def is_bug_type(issue_type: object) -> bool:
    return str(issue_type or "").strip().lower() in BUG_TYPES


def bug_refusal(key: str, *, described: bool = False) -> tuple[str, list[str]]:
    """The reply's situation and the next steps a person can choose from."""
    what = (
        f"The request on {key} describes something that is broken"
        if described
        else f"{key} is a Bug"
    )
    reason = (
        f"{what}, so no Epic, Story, Task or Sub-task was created and nothing was "
        "changed. A bug is investigated first: its cause, the affected code and a "
        "proposed fix."
    )
    options = [
        "To investigate it, add a comment that mentions the agent followed by: investigate",
        "For a Technical Design Document of the fix, mention the agent followed by: create TDD",
        "If this is really new work rather than a fault, describe the new behaviour and "
        "mention the agent followed by: build",
    ]
    return reason, options
