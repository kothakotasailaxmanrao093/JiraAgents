"""The reviewer does not re-raise gaps a ticket already lists.

BGV-108 (2026-09-25) carried three open questions written by Work Breakdown; its
review raised all three again, even with a system-prompt rule against it.
Fixtures are BGV-108's real questions and the real findings that repeated them.
"""

from __future__ import annotations

from typing import Any

from agent.delegated import run_delegated
from review.known_questions import drop_repeats, listed_open_questions
from review.prompt import build_user_message

BGV108 = """Acceptance criteria
- Given a selected slot, when an invitation is sent, then the candidate receives an email.
Related subtasks
- Develop Email Invitation System
Open questions — not stated in the requirement
- What should the email content include besides the slot and confirmation link?
- How is email delivery failure handled?
- What happens if the candidate's email address is incorrect?"""

DOCS = [{"source_label": "Target BGV-108 (description)", "kind": "requirement", "text": BGV108}]

REPEATS = [
    "There is no defined behavior for handling email delivery failures (e.g., retries, "
    "notifications to recruiter, logging) when sending the invitation.",
    "The flow and system behavior when the candidate's email address is incorrect "
    "(bounced emails, invalid format, or missing) are not specified.",
]
NEW = [
    "Behavior is unspecified if the candidate clicks the confirmation link after the "
    "48-hour confirmation window has elapsed and the slot may have been released.",
    "It is unclear whether the invitation email is triggered automatically upon slot "
    "selection, manually by the recruiter, or via some other event in the workflow.",
]


def test_the_listed_questions_are_read_from_the_target_description() -> None:
    assert listed_open_questions(DOCS) == [
        "What should the email content include besides the slot and confirmation link?",
        "How is email delivery failure handled?",
        "What happens if the candidate's email address is incorrect?",
    ]


def test_the_model_is_given_the_questions_by_name() -> None:
    message = build_user_message("BGV-108", "Send Invitation", DOCS, listed_open_questions(DOCS))
    assert "ALREADY LISTED ON THIS TICKET" in message
    assert "2. How is email delivery failure handled?" in message


def test_findings_that_repeat_a_listed_question_are_dropped_and_new_ones_kept() -> None:
    findings = [{"description": d} for d in REPEATS + NEW]
    kept = drop_repeats(findings, listed_open_questions(DOCS))
    assert [f["description"] for f in kept] == NEW


async def test_the_readiness_line_says_how_many_were_already_listed() -> None:
    async def run(payload: Any, execute: Any, workflow_id: Any = None) -> list[dict]:
        return [
            {
                "status": "success",
                "issue_key": "BGV-108",
                "findings": [{"category": "gap", "description": NEW[0]}],
                "readiness": {"level": "needs_minor_clarification", "score": 3},
                "known_questions": ["a", "b", "c"],
                "documents": DOCS,
                "target_bundle": {"key": "BGV-108", "description_text": BGV108},
            }
        ]

    async def execute(*a: Any, **k: Any) -> dict:  # pragma: no cover
        raise AssertionError

    out = await run_delegated({"issue_key": "BGV-108", "run_id": "r1"}, execute, run_review=run)
    assert "3 open question(s) already listed on the ticket" in out["summary"]
