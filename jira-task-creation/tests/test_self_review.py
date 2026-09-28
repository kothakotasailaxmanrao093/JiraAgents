"""D1 bar 1: the self-review gate, on the brief's own BGV-40 example.

BGV-40 is a COMPLETE requirement. Every avoidable mistake the brief lists is
fed to the gate here: asking "how many days before chasing?" when the input
says 5 working days, dropping the part-time rule, boilerplate criteria. Each
must be caught; a finding that quotes nothing in the input must be discarded.
"""

from __future__ import annotations

import json
from typing import Any

from src.classification import decompose
from src.classification.decompose import llm_breakdown
from src.classification.self_review import deterministic, drop_answered_questions, model_findings
from src.classification.validate import ProjectContext
from src.models.schemas import WorkBreakdown
from src.shared.rubric import BY_CODE, Avoidability

BGV40 = (
    "Let a recruiter raise an employment verification request for a candidate's "
    "last two employers. Each employer is tracked separately. If an employer does "
    "not respond in 5 working days, flag it to the recruiter. Part-time roles "
    "count as employers."
)


def _sub(title: str, check: str) -> dict[str, Any]:
    return {
        "title": title,
        "description": f"{title} for each of the candidate's last two employers.",
        "expected_outcome": f"{title} is delivered per employer.",
        "dependencies": "None",
        "completion_criteria": check,
    }


def _draft(open_questions: list[str], criteria: list[str] | None = None) -> dict[str, Any]:
    return {
        "classification": "Small",
        "analysis": "One capability: employment verification per employer.",
        "stories": [
            {
                "title": "Raise an employment verification request per employer",
                "user_story_statement": (
                    "As a recruiter, I want to raise a verification request for each of a "
                    "candidate's last two employers, so that I can track them independently."
                ),
                "description": "One request per employer, each with its own status.",
                "business_value": "Independent outcomes per employer.",
                "priority": "High",
                "estimated_complexity": "Medium",
                "dependencies": "None",
                "acceptance_criteria": criteria
                or [
                    "Given a candidate with two employers, when the recruiter raises "
                    "verification, then two requests are created, each with its own status",
                    "Given a request with no response for 5 working days, when the daily "
                    "check runs, then it is flagged to the recruiter",
                ],
                "subtasks": [
                    _sub(
                        "Create one request per employer",
                        "A candidate with two employers shows two requests with separate statuses.",
                    ),
                    _sub(
                        "Flag after 5 working days",
                        "A request unanswered for 5 working days appears flagged to its recruiter.",
                    ),
                ],
                "open_questions": open_questions,
            }
        ],
    }


def test_the_rubric_splits_avoidable_from_unavoidable() -> None:
    assert BY_CODE["ASKED_WHAT_WAS_STATED"].avoidability is Avoidability.AVOIDABLE
    assert BY_CODE["STATED_DETAIL_MISSING"].avoidability is Avoidability.AVOIDABLE
    assert BY_CODE["ABSENT_EVERYWHERE"].avoidability is Avoidability.UNAVOIDABLE


def test_boilerplate_is_caught_in_code() -> None:
    draft = _draft([], ["The behaviour matches the rules agreed with the product owner."])
    codes = [f.code for f in deterministic(WorkBreakdown.model_validate(draft))]
    assert "BOILERPLATE" in codes


async def test_asking_what_the_input_states_is_caught_and_the_question_removed() -> None:
    breakdown = WorkBreakdown.model_validate(_draft(["How many days before chasing?"]))

    async def chat(prompt: str) -> str:
        return json.dumps(
            {
                "asked_but_stated": [
                    {"question": "How many days before chasing?", "quote": "5 working days"}
                ]
            }
        )

    findings = await model_findings(BGV40, breakdown, chat)
    assert [f.code for f in findings] == ["ASKED_WHAT_WAS_STATED"]
    cleaned = drop_answered_questions(breakdown, findings)
    assert cleaned.stories[0].open_questions == []


async def test_a_dropped_stated_rule_is_caught_with_its_quote() -> None:
    breakdown = WorkBreakdown.model_validate(_draft([]))

    async def chat(prompt: str) -> str:
        return json.dumps({"stated_missing": [{"quote": "Part-time roles count as employers"}]})

    findings = await model_findings(BGV40, breakdown, chat)
    assert findings[0].code == "STATED_DETAIL_MISSING"
    assert "Part-time roles" in findings[0].as_instruction()


async def test_a_finding_that_quotes_nothing_in_the_input_is_discarded() -> None:
    """The self-reviewer must not over-reach either: no API was ever mentioned."""
    breakdown = WorkBreakdown.model_validate(_draft([]))

    async def chat(prompt: str) -> str:
        return json.dumps(
            {
                "stated_missing": [{"quote": "the notification API contract"}],
                "asked_but_stated": [
                    {"question": "Not a real question?", "quote": "5 working days"}
                ],
                "untraced": ["A sub-task that does not exist"],
            }
        )

    assert await model_findings(BGV40, breakdown, chat) == []


async def test_test_a_complete_input_ships_with_nothing_to_confirm(monkeypatch) -> None:
    """Test A: a faithful draft passes the gate first time — one breakdown call,
    one review call, no regeneration, no open questions."""
    prompts: list[str] = []

    async def chat(prompt: str) -> str:
        prompts.append(prompt)
        if prompt.startswith("Check this proposed Jira breakdown"):
            return json.dumps({"stated_missing": [], "asked_but_stated": [], "untraced": []})
        return json.dumps(_draft([]))

    monkeypatch.setattr(decompose, "_chat", chat)
    breakdown = await llm_breakdown(BGV40, ProjectContext())
    assert len(prompts) == 2
    assert breakdown.stories[0].open_questions == []
    assert breakdown._quality_notes == []


async def test_an_avoidable_finding_gets_exactly_one_regeneration(monkeypatch) -> None:
    prompts: list[str] = []
    fixed = _draft([])
    fixed["stories"][0]["acceptance_criteria"].append(
        "Given a candidate's part-time role, when verification is raised, then it counts as an employer"
    )

    async def chat(prompt: str) -> str:
        prompts.append(prompt)
        if prompt.startswith("Check this proposed Jira breakdown"):
            return json.dumps({"stated_missing": [{"quote": "Part-time roles count as employers"}]})
        if "Rewrite ONLY the completion criteria" in prompt:
            return json.dumps({"criteria": {}})
        return json.dumps(fixed if "Your previous answer was rejected" in prompt else _draft([]))

    monkeypatch.setattr(decompose, "_chat", chat)
    breakdown = await llm_breakdown(BGV40, ProjectContext())
    regenerations = [p for p in prompts if "Your previous answer was rejected" in p]
    assert len(regenerations) == 1
    assert "Part-time roles count as employers" in regenerations[0]
    assert any("part-time" in c for c in breakdown.stories[0].acceptance_criteria)


async def test_test_b_a_missing_detail_is_asked_not_invented(monkeypatch) -> None:
    """Test B: without "5 working days" the chase period must be ASKED."""
    incomplete = BGV40.replace(" in 5 working days", "")
    draft = _draft(["How many working days without a response before an employer is flagged?"])

    async def chat(prompt: str) -> str:
        if prompt.startswith("Check this proposed Jira breakdown"):
            return json.dumps({})
        return json.dumps(draft)

    monkeypatch.setattr(decompose, "_chat", chat)
    breakdown = await llm_breakdown(incomplete, ProjectContext())
    assert breakdown.stories[0].open_questions == [
        "How many working days without a response before an employer is flagged?"
    ]


def test_as_a_system_is_named_with_the_story_and_what_to_do() -> None:
    """BGV-4 (2026-09-25): "As a system, I want to send a consent link …"."""
    draft = _draft([])
    draft["stories"][0][
        "user_story_statement"
    ] = "As a system, I want to send a consent link to the candidate, so that they can consent."
    notes = [f.as_instruction() for f in deterministic(WorkBreakdown.model_validate(draft))]
    assert notes == [
        'Story "Raise an employment verification request per employer" is written '
        '"As a system" or "As a user" — it should name the person it serves.'
    ]


def test_the_prompt_forbids_as_a_system() -> None:
    from src.prompts.templates import SYSTEM_PROMPT

    assert 'never "As a system"' in SYSTEM_PROMPT


def test_a_named_machine_is_still_not_a_person() -> None:
    """BGV-27 / BGV-30: "As a billing system, I want …" slipped past an exact match."""
    for statement in (
        "As a billing system, I want to add completed checks to the invoice, so that clients pay.",
        "As the notification service, I want to email the client, so that they know.",
        "As a user, I want to download invoices, so that I can keep records.",
    ):
        draft = _draft([])
        draft["stories"][0]["user_story_statement"] = statement.replace("As the", "As a")
        codes = [f.code for f in deterministic(WorkBreakdown.model_validate(draft))]
        assert "NO_ACTOR" in codes, statement


def test_a_real_role_is_a_person() -> None:
    for role in ("client", "account manager", "billing officer", "system administrator"):
        draft = _draft([])
        draft["stories"][0][
            "user_story_statement"
        ] = f"As a {role}, I want to see my invoices, so that I can track payments."
        codes = [f.code for f in deterministic(WorkBreakdown.model_validate(draft))]
        assert "NO_ACTOR" not in codes, role
