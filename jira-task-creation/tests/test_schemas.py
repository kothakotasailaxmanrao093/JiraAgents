"""The structural contracts: enums, field sets, and the Small/Epic invariant."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.models.schemas import (
    Classification,
    Complexity,
    Epic,
    Priority,
    ResultStatus,
    Story,
    Subtask,
    WorkBreakdown,
    allow_source_files_from,
)


def make_subtask(**overrides) -> Subtask:
    base = {
        "title": "Define the language preference rules",
        "description": "Agree the supported languages and the default behaviour.",
        "expected_outcome": "A written, agreed set of rules.",
        "dependencies": "None",
        "completion_criteria": "The product owner has signed off the rules.",
    }
    return Subtask(**{**base, **overrides})


def make_story(**overrides) -> Story:
    base = {
        "title": "Update preferred language",
        "user_story_statement": (
            "As a registered user, I want to update my preferred language, "
            "so that the product speaks to me in a language I read."
        ),
        "description": "Let a signed-in user choose their preferred language.",
        "business_value": "Reduces support contacts from non-English speakers.",
        "priority": "Medium",
        "estimated_complexity": "Low",
        "dependencies": "None",
        "acceptance_criteria": ["The chosen language persists across sessions."],
        "subtasks": [make_subtask()],
    }
    return Story(**{**base, **overrides})


def make_epic(**overrides) -> Epic:
    base = {
        "business_objective": "Give users control over how they are notified.",
        "scope": ["Email notifications", "SMS notifications"],
        "out_of_scope": [],
        "priority": "High",
        "acceptance_criteria": ["Every channel can be enabled and disabled."],
        "jira_summary": "Notification channels and preferences",
    }
    return Epic(**{**base, **overrides})


# --- enums ----------------------------------------------------------------


def test_result_status_values():
    assert {s.value for s in ResultStatus} == {
        "VALIDATION_ERROR",
        "OUT_OF_SCOPE",
        "CLARIFICATION_REQUIRED",
        "READY_FOR_JIRA",
        # A question about the ticket is answered in prose; nothing is created.
        "EXPLAINED",
        "JIRA_CREATED",
        "JIRA_CREATION_FAILED",
    }


def test_classification_enum_values():
    assert [c.value for c in Classification] == ["Small", "Medium", "Large"]


def test_priority_enum_values():
    assert [p.value for p in Priority] == ["Low", "Medium", "High", "Critical"]


def test_complexity_enum_values():
    assert [c.value for c in Complexity] == ["Low", "Medium", "High"]


@pytest.mark.parametrize("bad", ["Urgent", "medium", "P1", ""])
def test_invalid_priority_is_rejected(bad):
    with pytest.raises(ValidationError):
        make_story(priority=bad)


@pytest.mark.parametrize("bad", ["Critical", "very high", ""])
def test_invalid_complexity_is_rejected(bad):
    with pytest.raises(ValidationError):
        make_story(estimated_complexity=bad)


# --- story contract -------------------------------------------------------


def test_story_statement_must_use_the_canonical_form():
    with pytest.raises(ValidationError, match="As a"):
        make_story(user_story_statement="Users should be able to change language.")


def test_story_statement_must_state_the_benefit():
    with pytest.raises(ValidationError, match="so that"):
        make_story(user_story_statement="As a user, I want to change my language setting today.")


def test_related_subtasks_is_derived_from_the_subtasks():
    story = make_story(
        subtasks=[
            make_subtask(title="First task and its rules"),
            make_subtask(title="Second task and its rules"),
        ]
    )
    assert story.related_subtasks == ["First task and its rules", "Second task and its rules"]


def test_story_requires_at_least_one_subtask():
    with pytest.raises(ValidationError):
        make_story(subtasks=[])


def test_story_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        make_story(story_points=3)


# --- subtask contract -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Modify auth_service.py.",
        "Update the handler in index.tsx and re-run it.",
        "Change UserRepository.java to add the field.",
    ],
)
def test_subtasks_may_not_name_source_files(text):
    with pytest.raises(ValidationError, match="behaviour, not source files"):
        make_subtask(description=text)


def test_a_filename_the_person_wrote_is_kept():
    """The no-filenames rule targets invention, not the person's own words.

    A request like "@Aetherion Update auth_service.py so drivers can log a
    break" used to abort the entire run, because the guard rejected the very
    filename the person had typed.
    """
    with allow_source_files_from("Update auth_service.py so drivers can log a break."):
        subtask = make_subtask(description="Update auth_service.py as the requirement states.")
    assert "auth_service.py" in subtask.description


def test_a_filename_the_person_did_not_write_is_still_refused():
    with allow_source_files_from("Update auth_service.py so drivers can log a break."):
        with pytest.raises(ValidationError, match="never mentioned"):
            make_subtask(description="Also refactor billing_engine.rb while you are there.")


def test_the_allowance_does_not_outlive_its_block():
    with allow_source_files_from("Update auth_service.py."):
        make_subtask(description="Update auth_service.py as the requirement states.")
    with pytest.raises(ValidationError, match="never mentioned"):
        make_subtask(description="Update auth_service.py as the requirement states.")


def test_behavioural_subtask_wording_is_accepted():
    subtask = make_subtask(
        description="Implement OTP verification behaviour according to the approved rules."
    )
    assert "OTP verification" in subtask.description


def test_subtask_dependencies_default_to_none():
    assert make_subtask().dependencies == "None"


def test_subtask_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        make_subtask(assignee="someone")


# --- epic contract --------------------------------------------------------


def test_epic_out_of_scope_defaults_to_not_specified():
    assert make_epic().out_of_scope == ["Not specified"]


def test_epic_rejects_extra_business_fields():
    for extra in ("epic_description", "epic_complexity", "epic_dependencies"):
        with pytest.raises(ValidationError):
            make_epic(**{extra: "something"})


def test_epic_related_user_stories_is_derived_from_the_stories():
    epic = make_epic()
    stories = [make_story(title="Email notifications"), make_story(title="SMS notifications")]
    assert epic.related_user_stories(stories) == ["Email notifications", "SMS notifications"]


def test_jira_summary_is_carried_but_is_not_a_business_field():
    epic = make_epic()
    business_fields = set(Epic.model_fields) - {"jira_summary"}
    assert business_fields == {
        "business_objective",
        "scope",
        "out_of_scope",
        "priority",
        "acceptance_criteria",
    }
    assert epic.jira_summary


# --- breakdown invariants -------------------------------------------------


def test_small_requirement_must_not_have_an_epic():
    with pytest.raises(ValidationError, match="must not produce an Epic"):
        WorkBreakdown(
            classification="Small",
            analysis="One focused change to a single setting.",
            epic=make_epic(),
            stories=[make_story()],
        )


def test_small_requirement_produces_exactly_one_story():
    with pytest.raises(ValidationError, match="exactly one Story"):
        WorkBreakdown(
            classification="Small",
            analysis="One focused change to a single setting.",
            stories=[make_story(), make_story()],
        )


@pytest.mark.parametrize("size", ["Medium", "Large"])
def test_medium_and_large_require_an_epic(size):
    with pytest.raises(ValidationError, match="must produce an Epic"):
        WorkBreakdown(
            classification=size,
            analysis="Several related capabilities across the product.",
            stories=[make_story(), make_story()],
        )


@pytest.mark.parametrize("size", ["Medium", "Large"])
def test_medium_and_large_require_multiple_stories(size):
    with pytest.raises(ValidationError, match="at least two Stories"):
        WorkBreakdown(
            classification=size,
            analysis="Several related capabilities across the product.",
            epic=make_epic(),
            stories=[make_story()],
        )


def test_issue_count_reports_the_hierarchy_size():
    breakdown = WorkBreakdown(
        classification="Medium",
        analysis="Two related capabilities.",
        epic=make_epic(),
        stories=[
            make_story(subtasks=[make_subtask(), make_subtask()]),
            make_story(subtasks=[make_subtask()]),
        ],
    )
    assert breakdown.issue_count() == {"epics": 1, "stories": 2, "subtasks": 3}
