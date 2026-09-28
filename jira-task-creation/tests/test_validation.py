"""The pre-decomposition gate: empty, meaningless, out-of-scope, ambiguous."""

from __future__ import annotations

import pytest

from src.classification.validate import (
    EMPTY_MESSAGE,
    ProjectContext,
    normalise,
    validate_input,
)
from src.models.schemas import ResultStatus


@pytest.mark.parametrize("raw", ["", "   ", "\n\t  \n", None])
def test_empty_input_is_a_validation_error(raw):
    verdict = validate_input(raw)
    assert verdict.status is ResultStatus.VALIDATION_ERROR
    assert verdict.reason == EMPTY_MESSAGE
    assert not verdict.may_proceed


def test_empty_input_names_the_missing_description():
    assert validate_input("").validation_errors == ["Requirement description is empty."]


@pytest.mark.parametrize(
    "raw",
    [
        "asdfgh",
        "asdfgh hello xyz",
        "hello hello hello",
        "!!!! ???? ....",
        "zzzz qqqq wwww",
        "1234567890 4321",
    ],
)
def test_meaningless_input_is_rejected(raw):
    verdict = validate_input(raw)
    assert verdict.status in {
        ResultStatus.VALIDATION_ERROR,
        ResultStatus.OUT_OF_SCOPE,
    }
    assert not verdict.may_proceed


@pytest.mark.parametrize(
    "raw",
    [
        "What is the weather today?",
        "Hi there!",
        "How are you doing this morning?",
        "Tell me a joke please",
    ],
)
def test_conversational_input_is_out_of_scope_or_rejected(raw):
    verdict = validate_input(raw)
    assert not verdict.may_proceed


def test_readable_text_with_no_action_asks_for_clarification():
    verdict = validate_input("The checkout page. Customers. Third quarter results.")
    assert verdict.status is ResultStatus.CLARIFICATION_REQUIRED
    assert verdict.clarifying_questions


def test_clarification_mentions_the_project_when_context_is_configured():
    context = ProjectContext(name="Atlas", description="Billing platform")
    verdict = validate_input("The checkout page. Customers. Quarterly figures.", context)
    assert verdict.status is ResultStatus.CLARIFICATION_REQUIRED
    assert any("project or module" in q for q in verdict.clarifying_questions)


@pytest.mark.parametrize(
    "raw",
    [
        "Allow users to update their preferred language.",
        "Send email and SMS notifications and allow users to manage notification preferences.",
        "Support mobile registration, OTP verification, login, token refresh, logout, "
        "password recovery, and active session management.",
    ],
)
def test_real_requirements_clear_the_gate(raw):
    assert validate_input(raw).may_proceed


def test_oversized_input_is_rejected_without_truncation():
    verdict = validate_input("Add a reporting feature. " * 2000)
    assert verdict.status is ResultStatus.VALIDATION_ERROR
    assert "characters" in verdict.reason


def test_normalise_handles_none_and_non_strings():
    assert normalise(None) == ""
    assert normalise("  padded  ") == "padded"
    assert normalise(42) == "42"


def test_project_context_reads_the_environment(monkeypatch):
    monkeypatch.setenv("LTW_PROJECT_NAME", "Atlas")
    monkeypatch.setenv("LTW_ALLOWED_DOMAINS", " billing , onboarding ")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "abc")
    context = ProjectContext.from_env()
    assert context.name == "Atlas"
    assert context.allowed_domains == ("billing", "onboarding")
    assert context.jira_project_key == "ABC"
    assert "billing" in context.as_prompt_text()


def test_unconfigured_context_says_so_rather_than_inventing_a_domain():
    text = ProjectContext().as_prompt_text()
    assert "No project context is configured" in text


def test_everyday_product_verbs_are_not_refused_as_non_actionable() -> None:
    """ "Log a rest break." was refused as "does not describe any work to be done".

    "log" was simply absent from the verb list, so the request was rejected
    while "Record a rest break." — the same requirement, one synonym apart —
    was accepted.
    """
    for text in (
        "Log a rest break.",
        "Log a vehicle fault.",
        "Sign in with a one-time code.",
        "Monitor fuel spend per depot.",
        "Dispatch a driver to a job.",
        "Confirm a booking by email.",
        "Scan a parcel barcode.",
    ):
        assert validate_input(text).status is ResultStatus.READY_FOR_JIRA, text


def test_widening_the_verb_list_did_not_open_the_gate() -> None:
    """The counterweight to the test above: junk must still be refused."""
    for text in ("hello", "asdkjhasd kjashd", "?????", "ok thanks", "", "123456"):
        assert validate_input(text).status is ResultStatus.VALIDATION_ERROR, text
    assert validate_input("What is the weather today?").status is ResultStatus.OUT_OF_SCOPE


# --- a wish is not a requirement --------------------------------------------
# Found live: "@Aetherion I am feeling hungry and I want to eat biryani" was
# accepted and produced a Story with that sentence as its title. It matched on
# the word "want".


@pytest.mark.parametrize(
    "text",
    [
        "I am feeling hungry and I want to eat biryani",
        "I want to go home early today",
        "We need a decision on this",
        "Someone should look at this",
    ],
)
def test_a_wish_word_alone_does_not_make_a_requirement(text: str) -> None:
    assert validate_input(text).status is not ResultStatus.READY_FOR_JIRA


@pytest.mark.parametrize(
    "text",
    [
        # A wish word plus a real work verb is a requirement, and must stay one.
        "Users should be able to reset their password.",
        "We need to track fuel spend per vehicle.",
        "Drivers need a mobile checklist. It should be completed before each trip.",
        "Allow users to update their preferred language.",
        "Log a rest break.",
    ],
)
def test_a_wish_word_beside_real_work_is_still_accepted(text: str) -> None:
    assert validate_input(text).status is ResultStatus.READY_FOR_JIRA


def test_past_tense_verbs_are_recognised() -> None:
    """ "completed" cut to "complet", which is not a word, so it matched nothing.

    That hid a real requirement — "it should be completed before each trip" —
    behind two wish words.
    """
    from src.classification.validate import _verb_stem

    assert _verb_stem("completed") == "complete"
    assert _verb_stem("created") == "create"
    assert _verb_stem("updated") == "update"
    assert _verb_stem("tracked") == "track"


def test_the_no_requirement_placeholder_is_never_read_as_a_requirement() -> None:
    """It contains "provided", which stems to the action word "provide"."""
    from src.context.ingest import NO_REQUIREMENT_STATED

    assert validate_input(NO_REQUIREMENT_STATED).status is not ResultStatus.READY_FOR_JIRA


def test_existing_tickets_are_cited_by_key_not_title_alone():
    """A clarifying question asking "does this differ from <ticket>?" is
    useless without a key the reader can open — the model can only cite what
    it is given, and titles alone gave it nothing to cite."""
    context = ProjectContext.from_jira(
        {"name": "BGV", "description": "Background checks.", "key": "BGV"},
        [{"key": "BGV-19", "summary": "Record candidate consent"}],
    )
    assert context.existing_titles == ("BGV-19 — Record candidate consent",)
    text = context.as_prompt_text(for_breakdown=True)
    assert "BGV-19" in text
    assert "Record candidate consent" in text


def test_a_ticket_with_no_key_still_shows_its_title():
    """Defensive: a malformed existing-issue entry must not crash context
    building, and should still surface whatever title it has."""
    context = ProjectContext.from_jira(
        {"name": "BGV"},
        [{"summary": "Untitled work"}],
    )
    assert context.existing_titles == ("Untitled work",)
