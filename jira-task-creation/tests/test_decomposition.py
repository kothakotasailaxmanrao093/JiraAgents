"""Classification and decomposition, on both the LLM and heuristic paths."""

from __future__ import annotations

import json

import pytest

from src.classification import decompose as decomposer
from src.classification.decompose import (
    DecompositionError,
    build_breakdown,
    classify,
    heuristic_breakdown,
    llm_breakdown,
    llm_triage,
    split_capabilities,
    triage,
)
from src.classification.validate import ProjectContext
from src.models.schemas import NOT_SPECIFIED, Classification, ResultStatus

SMALL = "Allow users to update their preferred language."
MEDIUM = "Send email and SMS notifications and allow users to manage notification preferences."
LARGE = (
    "Support mobile registration, OTP verification, login, token refresh, logout, "
    "password recovery, and active session management."
)

CONTEXT = ProjectContext(name="Atlas", description="Customer self-service portal")


@pytest.fixture
def fake_chat(monkeypatch):
    """Replace the gateway round-trip with a canned response."""

    def _install(response: str | Exception):
        async def _chat(_prompt: str) -> str:
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(decomposer, "_chat", _chat)

    return _install


# --- classification -------------------------------------------------------


def test_small_requirement_is_classified_small():
    assert classify(SMALL) is Classification.SMALL


def test_medium_requirement_is_classified_medium():
    assert classify(MEDIUM) is Classification.MEDIUM


def test_large_requirement_is_classified_large():
    assert classify(LARGE) is Classification.LARGE


def test_classification_follows_scope_not_description_length():
    verbose_single = (
        "We would like to allow every signed-in customer to update their preferred "
        "language from the account settings area, because today the only way to "
        "change it is to contact support, which is slow for everyone involved and "
        "generates avoidable tickets every single week of the year."
    )
    assert classify(verbose_single) is Classification.SMALL


def test_bulleted_requirements_are_split_by_bullet():
    bulleted = (
        "Rework onboarding:\n- collect the company profile\n- verify the domain\n- invite teammates"
    )
    assert len(split_capabilities(bulleted)) == 3


# --- heuristic breakdown --------------------------------------------------


def test_heuristic_small_breakdown_has_no_epic():
    breakdown = heuristic_breakdown(SMALL, CONTEXT)
    assert breakdown.classification is Classification.SMALL
    assert breakdown.epic is None
    assert len(breakdown.stories) == 1
    assert breakdown.stories[0].subtasks


@pytest.mark.parametrize("requirement", [MEDIUM, LARGE])
def test_heuristic_medium_and_large_have_an_epic_and_many_stories(requirement):
    breakdown = heuristic_breakdown(requirement, CONTEXT)
    assert breakdown.epic is not None
    assert len(breakdown.stories) >= 2
    assert all(story.subtasks for story in breakdown.stories)


def test_heuristic_epic_out_of_scope_is_not_invented():
    breakdown = heuristic_breakdown(MEDIUM, CONTEXT)
    assert breakdown.epic.out_of_scope == ["Not specified"]


def test_heuristic_subtasks_never_name_source_files():
    breakdown = heuristic_breakdown(LARGE, CONTEXT)
    for story in breakdown.stories:
        for subtask in story.subtasks:
            blob = " ".join(
                [
                    subtask.title,
                    subtask.description,
                    subtask.expected_outcome,
                    subtask.completion_criteria,
                ]
            )
            assert ".py" not in blob and ".java" not in blob and ".ts" not in blob


def test_heuristic_stories_use_the_canonical_statement():
    for story in heuristic_breakdown(MEDIUM, CONTEXT).stories:
        lowered = story.user_story_statement.lower()
        assert lowered.startswith("as a")
        assert "i want" in lowered and "so that" in lowered


# --- LLM triage -----------------------------------------------------------


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ("VALID", ResultStatus.READY_FOR_JIRA),
        ("INVALID", ResultStatus.VALIDATION_ERROR),
        ("OUT_OF_SCOPE", ResultStatus.OUT_OF_SCOPE),
        ("CLARIFICATION_REQUIRED", ResultStatus.CLARIFICATION_REQUIRED),
    ],
)
async def test_llm_triage_maps_each_verdict(fake_chat, verdict, expected):
    fake_chat(json.dumps({"verdict": verdict, "reason": "because", "questions": ["What?"]}))
    result = await llm_triage("Add approval functionality.", CONTEXT)
    assert result.status is expected


async def test_ambiguous_requirement_returns_the_questions(fake_chat):
    fake_chat(
        json.dumps(
            {
                "verdict": "CLARIFICATION_REQUIRED",
                "reason": "The item under approval is unknown.",
                "questions": [
                    "What type of item is being approved?",
                    "Which role can approve or reject it?",
                    "What should happen after approval or rejection?",
                ],
            }
        )
    )
    result = await llm_triage("Add approval functionality.", CONTEXT)
    assert result.status is ResultStatus.CLARIFICATION_REQUIRED
    assert len(result.clarifying_questions) == 3


async def test_clarification_without_questions_still_asks_something(fake_chat):
    fake_chat(json.dumps({"verdict": "CLARIFICATION_REQUIRED", "reason": "", "questions": []}))
    result = await llm_triage("Add approval functionality.", CONTEXT)
    assert result.clarifying_questions


async def test_unknown_verdict_is_an_error(fake_chat):
    fake_chat(json.dumps({"verdict": "MAYBE"}))
    with pytest.raises(DecompositionError):
        await llm_triage(SMALL, CONTEXT)


async def test_triage_falls_back_to_proceed_when_the_gateway_is_down(fake_chat):
    fake_chat(RuntimeError("gateway unreachable"))
    verdict, generator = await triage(SMALL, CONTEXT)
    assert verdict.status is ResultStatus.READY_FOR_JIRA
    assert generator == "heuristic"


async def test_triage_propagates_when_the_llm_is_marked_required(fake_chat, monkeypatch):
    monkeypatch.setenv("LTW_REQUIRE_LLM", "true")
    fake_chat(RuntimeError("gateway unreachable"))
    with pytest.raises(RuntimeError):
        await triage(SMALL, CONTEXT)


# --- LLM breakdown --------------------------------------------------------

VALID_LLM_SMALL = {
    "classification": "Small",
    "analysis": "One focused change to an existing account setting.",
    "stories": [
        {
            "title": "Update preferred language",
            "user_story_statement": (
                "As a registered user, I want to update my preferred language, "
                "so that the product speaks to me in a language I read."
            ),
            "description": "Let a signed-in user choose their preferred language.",
            "business_value": "Fewer support contacts about language.",
            "priority": "Medium",
            "estimated_complexity": "Low",
            "dependencies": "None",
            "acceptance_criteria": ["The chosen language persists across sessions."],
            "subtasks": [
                {
                    "title": "Agree the supported language list",
                    "description": "Confirm which languages are offered and the default.",
                    "expected_outcome": "An agreed list of supported languages.",
                    "dependencies": "None",
                    "completion_criteria": "The product owner signs off the list.",
                }
            ],
        }
    ],
}


async def test_llm_breakdown_parses_and_validates(fake_chat):
    fake_chat(json.dumps(VALID_LLM_SMALL))
    breakdown = await llm_breakdown(SMALL, CONTEXT)
    assert breakdown.classification is Classification.SMALL
    assert breakdown.epic is None


async def test_llm_breakdown_tolerates_code_fences(fake_chat):
    fake_chat("```json\n" + json.dumps(VALID_LLM_SMALL) + "\n```")
    assert (await llm_breakdown(SMALL, CONTEXT)).classification is Classification.SMALL


async def test_llm_breakdown_drops_a_null_epic_on_a_small_result(fake_chat):
    fake_chat(json.dumps({**VALID_LLM_SMALL, "epic": None}))
    assert (await llm_breakdown(SMALL, CONTEXT)).epic is None


async def test_llm_breakdown_rejects_a_small_result_carrying_an_epic(fake_chat):
    payload = {
        **VALID_LLM_SMALL,
        "epic": {
            "business_objective": "Something the model invented for a small change.",
            "scope": ["a"],
            "out_of_scope": ["Not specified"],
            "priority": "Medium",
            "acceptance_criteria": ["b"],
            "jira_summary": "Unwanted epic",
        },
    }
    fake_chat(json.dumps(payload))
    with pytest.raises(DecompositionError, match="did not match the breakdown format"):
        await llm_breakdown(SMALL, CONTEXT)


async def test_llm_breakdown_rejects_a_subtask_naming_a_source_file(fake_chat):
    payload = json.loads(json.dumps(VALID_LLM_SMALL))
    payload["stories"][0]["subtasks"][0]["description"] = "Modify auth_service.py."
    fake_chat(json.dumps(payload))
    with pytest.raises(DecompositionError, match="did not match the breakdown format"):
        await llm_breakdown(SMALL, CONTEXT)


async def test_llm_breakdown_rejects_non_json(fake_chat):
    fake_chat("Sure! Here is your breakdown, in prose.")
    with pytest.raises(DecompositionError):
        await llm_breakdown(SMALL, CONTEXT)


async def test_without_the_model_nothing_is_built(fake_chat):
    """Owner decision (2026-09-25): no boilerplate fallback — nothing is created."""
    fake_chat(RuntimeError("gateway unreachable"))
    with pytest.raises(DecompositionError, match="gateway unreachable"):
        await build_breakdown(MEDIUM, CONTEXT)


async def test_build_breakdown_prefers_the_llm_when_it_answers(fake_chat):
    fake_chat(json.dumps(VALID_LLM_SMALL))
    breakdown, generator = await build_breakdown(SMALL, CONTEXT)
    assert generator == "llm"
    assert breakdown.stories[0].title == "Update preferred language"


def test_bullet_markers_do_not_reach_a_jira_summary() -> None:
    """An Epic built from a list was summarised with its markers still in it:
    "Add: - driver shift roster - fuel spend by depot - tyre replacement log".
    """
    from src.classification.decompose import _flatten_list_markers

    assert (
        _flatten_list_markers(
            "Please add:\n- driver shift roster\n- fuel spend by depot\n- tyre replacement log"
        )
        == "Please add: driver shift roster, fuel spend by depot and tyre replacement log"
    )
    assert _flatten_list_markers("- only one bullet") == "only one bullet"
    # Ordinary prose is left alone.
    assert _flatten_list_markers("Allow drivers to log a break.") == (
        "Allow drivers to log a break."
    )


# --- work ruled out in discussion -------------------------------------------
# AUDIT D3: "SMS is out of scope for this release, email only" posted as a
# follow-up comment was stripped before decomposition, so the SMS work was
# built anyway. Every human on the ticket could read the decision; nothing in
# the agent could.


def _notifications_issue(comments):
    from src.models.schemas import CommentInfo, SourceIssue

    return SourceIssue(
        key="KS-12",
        summary="Notifications",
        issue_type="Task",
        description="Send email and SMS notifications, and let users manage preferences.",
        comments=[CommentInfo(**c) for c in comments],
        trigger_comment_id="9",
        trigger_comment_author="Priya",
        trigger_comment_body="@Aetherion please break this down into stories",
    )


TRIGGER = {"id": "9", "author": "Priya", "body": "@Aetherion please break this down into stories"}


def _titles(comments) -> list[str]:
    from src.classification.validate import ProjectContext
    from src.context.ingest import assemble_requirement

    requirement, _ = assemble_requirement(_notifications_issue(comments))
    return [s.title for s in heuristic_breakdown(requirement, ProjectContext()).stories]


def test_work_a_comment_ruled_out_is_not_built() -> None:
    titles = _titles(
        [{"id": "1", "author": "Ann", "body": "SMS is out of scope for this release, email only."}]
        + [TRIGGER]
    )
    assert not any("SMS" in t.upper() for t in titles), titles
    assert any("email" in t.lower() for t in titles), "the rest of the work must survive"


def test_without_that_comment_the_same_work_is_built() -> None:
    """The counterweight: the filter must not fire on its own."""
    titles = _titles([TRIGGER])
    assert any("SMS" in t.upper() for t in titles), titles


@pytest.mark.parametrize(
    "wording",
    [
        "SMS is out of scope for this release.",
        "SMS has been dropped.",
        "SMS is deferred.",
        "Let's skip SMS for now.",
    ],
)
def test_the_usual_ways_of_saying_it_are_recognised(wording: str) -> None:
    titles = _titles([{"id": "1", "author": "Ann", "body": wording}, TRIGGER])
    assert not any("SMS" in t.upper() for t in titles), (wording, titles)


def test_an_exclusion_that_would_empty_the_breakdown_is_ignored() -> None:
    """Too broad to trust: a human decides rather than getting nothing back."""
    titles = _titles(
        [{"id": "1", "author": "Ann", "body": "Notifications are out of scope."}, TRIGGER]
    )
    assert titles, "the run must still produce work for a person to judge"


def test_the_agents_own_reply_cannot_rule_work_out() -> None:
    from src.jira.api import AGENT_FOOTER

    titles = _titles(
        [
            {"id": "1", "author": "bot", "body": f"SMS is out of scope.\n{AGENT_FOOTER}"},
            TRIGGER,
        ]
    )
    assert any("SMS" in t.upper() for t in titles), titles


def test_an_acronym_keeps_its_capitals() -> None:
    """Lowercasing the first letter produced Stories reading "sMS notifications"."""
    from src.classification.decompose import to_infinitive

    assert to_infinitive("SMS notifications") == "SMS notifications"
    assert to_infinitive("API access") == "API access"
    # Ordinary words still lowercase, so "I want to ..." reads correctly.
    assert to_infinitive("Send email") == "send email"
    assert to_infinitive("To send email") == "send email"


# --- a list of values is not a list of work ---------------------------------
# AUDIT D4: "A request moves through four states: Draft, Submitted, Approved,
# and Rejected" became four Stories, three of them named after a status.


@pytest.mark.parametrize(
    "requirement",
    [
        "A request moves through four states: Draft, Submitted, Approved, and Rejected.",
        "Add a priority field with values: Low, Medium, High and Critical.",
        "Support roles: admin, manager and driver.",
        "Each job has statuses: queued, running or failed.",
    ],
)
def test_values_listed_after_a_noun_stay_one_capability(requirement: str) -> None:
    assert len(split_capabilities(requirement)) == 1, split_capabilities(requirement)


def test_the_listed_values_are_still_in_the_text() -> None:
    """Held together, not thrown away — the states are part of the requirement."""
    (only,) = split_capabilities("A request moves through states: Draft, Approved and Rejected.")
    for state in ("Draft", "Approved", "Rejected"):
        assert state in only


@pytest.mark.parametrize(
    "requirement, expected",
    [
        (
            "Track servicing, tyre replacement, insurance renewal, fuel spend, "
            "licence expiry, and accident reports.",
            6,
        ),
        (
            "Let managers schedule a service, and notify the depot when a vehicle "
            "is off the road.",
            2,
        ),
        ("Please add:\n- driver shift roster\n- fuel spend by depot\n- tyre log", 3),
    ],
)
def test_real_capability_lists_still_split(requirement: str, expected: int) -> None:
    """The counterweight: holding value-lists together must not fuse real work."""
    assert len(split_capabilities(requirement)) == expected


# --- an Epic must describe what is under it ---------------------------------
# Found live on FL-85: SMS was correctly excluded from the Stories, but the Epic
# was still titled "Send email and SMS notifications…", telling a reader the SMS
# work was covered when it had been deliberately dropped.


def _epic_for(comments):
    from src.classification.validate import ProjectContext
    from src.context.ingest import assemble_requirement

    requirement, _ = assemble_requirement(_notifications_issue(comments))
    return heuristic_breakdown(requirement, ProjectContext())


EXCLUDE_SMS = {"id": "1", "author": "Ann", "body": "SMS is out of scope for this release."}


def test_an_epic_does_not_advertise_work_that_was_ruled_out() -> None:
    breakdown = _epic_for([EXCLUDE_SMS, TRIGGER])
    assert breakdown.epic is not None
    assert "SMS" not in breakdown.epic.jira_summary.upper()
    assert "SMS" not in breakdown.epic.business_objective.upper()
    assert not any("SMS" in item.upper() for item in breakdown.epic.scope)


def test_what_was_ruled_out_is_recorded_as_out_of_scope() -> None:
    """The Epic has a field for exactly this; it used to say "Not specified"."""
    breakdown = _epic_for([EXCLUDE_SMS, TRIGGER])
    assert any("sms" in item.lower() for item in breakdown.epic.out_of_scope)


def test_an_epic_without_exclusions_is_unchanged() -> None:
    breakdown = _epic_for([TRIGGER])
    assert "SMS" in breakdown.epic.jira_summary.upper()
    assert breakdown.epic.out_of_scope == [NOT_SPECIFIED]


def test_sizing_counts_only_the_work_that_survives() -> None:
    """Excluding down to one capability is Small, so it must not carry an Epic."""
    breakdown = _epic_for(
        [
            {
                "id": "1",
                "author": "Ann",
                "body": "SMS is out of scope. Preferences are out of scope.",
            },
            TRIGGER,
        ]
    )
    assert breakdown.classification is Classification.SMALL
    assert breakdown.epic is None
    assert len(breakdown.stories) == 1


def test_carrying_a_verb_across_a_list_keeps_acronyms_intact() -> None:
    """ "Send email and SMS notifications" produced "send sMS notifications"."""
    # A comma makes it a real enumeration; a lone "and" would join one phrase.
    assert split_capabilities(
        "Send email and SMS notifications, and let users manage preferences."
    ) == [
        "Send email",
        "send SMS notifications",
        "let users manage preferences",
    ]
    assert split_capabilities("Support PDF and CSV exports, and email alerts.") == [
        "Support PDF",
        "support CSV exports",
        "support email alerts",
    ]
    # Ordinary words still lowercase when the verb is carried across.
    assert split_capabilities("Track servicing, tyre replacement, and fuel spend.") == [
        "Track servicing",
        "track tyre replacement",
        "track fuel spend",
    ]


def test_a_paragraph_of_its_own_is_its_own_capability() -> None:
    """A complete sentence on its own line must not be swallowed.

    The continuation rule merges a fragment that does not start with a verb.
    That is right mid-sentence, and wrong across a paragraph break: a ticket
    description and a linked page became one Story joined by "and".
    """
    assert split_capabilities(
        "Customers may store one saved card.\n\nCustomers may also see a receipt."
    ) == ["Customers may store one saved card", "Customers may also see a receipt"]


@pytest.mark.parametrize(
    "requirement, expected",
    [
        # Modifiers inside one line still merge — that is the case the
        # continuation rule exists for.
        ("Create a dashboard with all the drivers and vehicles, with all the trackers.", 1),
        (
            "Track servicing, tyre replacement, insurance renewal, fuel spend, "
            "licence expiry, and accident reports.",
            6,
        ),
        ("A request moves through four states: Draft, Submitted, Approved, and Rejected.", 1),
    ],
)
def test_single_line_splitting_is_unchanged(requirement: str, expected: int) -> None:
    assert len(split_capabilities(requirement)) == expected


# --- BGV-9: one broken rule must not fail the whole build -----------------


def _with_statement(statement: str) -> dict:
    payload = json.loads(json.dumps(VALID_LLM_SMALL))
    payload["stories"][0]["user_story_statement"] = statement
    return payload


@pytest.mark.parametrize(
    "statement",
    [
        # The exact BGV-9 form: good English, rejected until 2026-09-24.
        "As a recruiter, I want candidates to give digital consent, so that checks are lawful.",
        "As an officer, I want the system to block checks, so that consent is respected.",
        "As a recruiter, I want to see consent status, so that I know who can be checked.",
    ],
)
def test_good_story_wording_is_accepted(statement: str) -> None:
    from src.models.schemas import WorkBreakdown

    WorkBreakdown.model_validate(_with_statement(statement))


def test_a_bare_verb_after_i_want_is_flagged_but_valid() -> None:
    from src.models.schemas import WorkBreakdown, statement_reads_badly

    statement = "As a recruiter, I want send reminders to referees, so that they reply."
    WorkBreakdown.model_validate(_with_statement(statement))
    assert statement_reads_badly(statement)


@pytest.mark.parametrize(
    "statement",
    [
        # BGV-41 (2026-09-25): rejected twice, and the whole build failed.
        "As a billing officer, I want invoices to be sent to clients, so that they pay.",
        "As a recruiter, I want candidates to give digital consent, so that checks are lawful.",
        "As a recruiter, I want to see consent status, so that I know who can be checked.",
    ],
)
def test_good_english_is_not_flagged(statement: str) -> None:
    from src.models.schemas import statement_reads_badly

    assert not statement_reads_badly(statement)


async def test_a_broken_answer_is_retried_once_with_the_reason(monkeypatch) -> None:
    prompts: list[str] = []
    bad = _with_statement("As a recruiter, I want send reminders to referees, so that they reply.")

    async def _chat(prompt: str) -> str:
        prompts.append(prompt)
        if prompt.startswith("Check this proposed Jira breakdown"):
            return json.dumps({})
        if "Rewrite ONLY the completion criteria" in prompt:
            return json.dumps({"criteria": {}})
        rejected = "Your previous answer was rejected" in prompt
        return json.dumps(VALID_LLM_SMALL if rejected else bad)

    monkeypatch.setattr(decomposer, "_chat", _chat)
    breakdown = await llm_breakdown(SMALL, CONTEXT)
    assert breakdown.stories
    retry = next(p for p in prompts if "Your previous answer was rejected" in p)
    assert "bare verb" in retry


async def test_awkward_wording_twice_still_builds(monkeypatch) -> None:
    """Wording never fails a build: asked once, then accepted (BGV-41)."""
    bad = json.dumps(_with_statement("As a recruiter, I want send it, so that it goes out."))

    async def _chat(prompt: str) -> str:
        return bad

    monkeypatch.setattr(decomposer, "_chat", _chat)
    breakdown = await llm_breakdown(SMALL, CONTEXT)
    assert breakdown.stories
