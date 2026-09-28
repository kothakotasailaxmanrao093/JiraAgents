"""Rendering this agent's Jira issue descriptions as ADF.

The Atlassian Document Format machinery itself — building nodes, assembling a
document from ``(heading, body)`` pairs, and reading ADF back to structured
text — lives in :mod:`src.shared.adf`, shared with the review agent. It had to
be shared: the review agent's own reader flattened a whole ticket onto one line,
which is fatal when the job is to notice that acceptance criteria are missing.

What stays here is this agent's contract: exactly which named fields an Epic, a
Story and a Sub-task description carry, and in what order.
"""

from __future__ import annotations

from typing import Any

from src.models.schemas import Epic, Story
from src.shared.adf import _ADF_TEXT_BLOCKS, _adf_inline, _adf_lines
from src.shared.adf import blocks_to_doc as adf
from src.shared.adf import to_text as _plain_text

__all__ = [
    "_ADF_TEXT_BLOCKS",
    "_adf_inline",
    "_adf_lines",
    "_plain_text",
    "adf",
    "epic_description",
    "story_description",
    "subtask_description",
]


def epic_description(epic: Epic, story_titles: list[str]) -> dict[str, Any]:
    """Render exactly the six business fields of the Epic contract."""
    return adf(
        [
            ("Business objective", epic.business_objective),
            ("Scope", epic.scope),
            ("Out-of-scope items", epic.out_of_scope),
            ("Priority", epic.priority.value),
            ("Acceptance criteria", epic.acceptance_criteria),
            ("Related user stories", story_titles),
        ]
    )


def story_description(story: Story) -> dict[str, Any]:
    """Render the nine fields of the Story contract, then its open questions."""
    # Only when there are any: an empty list would still render its heading.
    questions = (
        [("Open questions — not stated in the requirement", story.open_questions)]
        if story.open_questions
        else []
    )
    return adf(
        [
            ("User story", story.user_story_statement),
            ("Description", story.description),
            ("Business value", story.business_value),
            ("Priority", story.priority.value),
            ("Estimated complexity", story.estimated_complexity.value),
            ("Dependencies", story.dependencies),
            ("Acceptance criteria", story.acceptance_criteria),
            ("Related subtasks", story.related_subtasks),
            *questions,
        ]
    )


def subtask_description(subtask: Any) -> dict[str, Any]:
    """Render exactly the five fields of the Subtask contract."""
    return adf(
        [
            ("Description", subtask.description),
            ("Expected outcome", subtask.expected_outcome),
            ("Dependencies", subtask.dependencies),
            ("Completion criteria", subtask.completion_criteria),
        ]
    )
