"""Requirement assembly from a read Jira issue."""

from __future__ import annotations

import json

import pytest

from src.context.ingest import (
    assemble_requirement,
    build_sections,
    describe_issue,
    is_pointer_comment,
    is_question_comment,
    max_context_chars,
)
from src.models.schemas import (
    AttachmentText,
    CommentInfo,
    HistoryEntry,
    LinkedIssue,
    SourceIssue,
    WorklogEntry,
)


def full_issue() -> SourceIssue:
    return SourceIssue(
        key="KS-12",
        summary="Notification preferences",
        trigger_comment_id="9001",
        trigger_comment_author="Priya",
        trigger_comment_body=(
            "@Aetherion Send email and SMS notifications and let users manage " "preferences."
        ),
        description="Send email and SMS notifications and let users manage preferences.",
        attachments=[
            AttachmentText(filename="spec.pdf", text="Channel matrix and opt-out rules."),
            AttachmentText(filename="logo.png", note="Image attachment skipped."),
        ],
        comments=[CommentInfo(author="Ann", created="2026-09-02", body="Opt-out is mandatory.")],
        linked_issues=[LinkedIssue(key="KS-3", summary="Email service", relationship="blocks")],
        history=[
            HistoryEntry(
                created="2026-09-03", author="Ravi", field="status", to_value="In Progress"
            )
        ],
        worklogs=[WorklogEntry(author="Ravi", started="2026-09-03", time_spent="2h")],
    )


def test_every_populated_section_is_rendered() -> None:
    names = [name for name, _ in build_sections(full_issue())]
    assert names == ["description", "attachments", "comments", "links", "history", "worklogs"]


def test_the_request_is_always_first() -> None:
    text, _ = assemble_requirement(full_issue())
    assert text.startswith("## Stated requirement (")


def test_unreadable_attachments_are_excluded() -> None:
    text, _ = assemble_requirement(full_issue())
    assert "spec.pdf" in text
    assert "logo.png" not in text


def test_sections_are_labelled_so_discussion_is_not_read_as_requirement() -> None:
    text, _ = assemble_requirement(full_issue())
    assert "Discussion on the ticket (context, not necessarily requirements)" in text
    assert "Linked work items (already exist — do not recreate)" in text
    assert "Recent field history (context only)" in text


def test_a_manual_run_without_a_comment_uses_the_description() -> None:
    """scripts/run_issue.py --text passes the requirement in directly."""
    text, _ = assemble_requirement(
        SourceIssue(key="KS-1", summary="x", description="Add dark mode")
    )
    assert "Add dark mode" in text


def test_an_empty_issue_says_so_rather_than_producing_nothing() -> None:
    text, _ = assemble_requirement(SourceIssue(key="KS-1"))
    assert "(no description provided)" in text


def test_sections_are_dropped_lowest_value_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # A budget that fits the description and little else.
    monkeypatch.setenv("LTW_CONTEXT_MAX_CHARS", "1000")
    source = full_issue()
    source.comments = [CommentInfo(author="Ann", body="x" * 400)]
    source.history = [HistoryEntry(field="status", to_value="y" * 400)]
    source.worklogs = [WorklogEntry(time_spent="z" * 400)]

    text, notes = assemble_requirement(source)

    assert "Work logged so far" not in text, "work logs should be dropped first"
    assert any("worklogs" in note for note in notes)
    assert text.startswith("## Stated requirement"), "the description is never dropped"


def test_an_oversized_description_is_truncated_and_the_note_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LTW_CONTEXT_MAX_CHARS", "1000")
    source = SourceIssue(key="KS-1", summary="Big", description="d" * 5_000)
    text, notes = assemble_requirement(source)
    assert len(text) == 1_000
    assert any("truncated" in note for note in notes)


def test_context_budget_is_configurable_and_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTW_CONTEXT_MAX_CHARS", "50000")
    assert max_context_chars() == 50_000
    monkeypatch.setenv("LTW_CONTEXT_MAX_CHARS", "10")
    assert max_context_chars() == 1_000, "a nonsensically small budget is floored"
    monkeypatch.setenv("LTW_CONTEXT_MAX_CHARS", "not-a-number")
    assert max_context_chars() == 24_000


def test_source_counts_report_only_usable_attachments() -> None:
    counts = full_issue().source_counts()
    assert counts == {
        "attachments": 1,
        "confluence_pages": 0,
        "comments": 1,
        "history": 1,
        "worklogs": 1,
        "linked_issues": 1,
    }


# --- assembly artefacts must never become work ------------------------------
# Found in the first live webhook run against TT2: the assembled markdown was
# fed straight to the heuristic decomposer, which turned the section heading and
# the "Aetherion please break this down." instruction into Stories of their own
# (TT2-87, TT2-91).


def test_section_headings_never_become_capabilities() -> None:
    from src.classification.decompose import split_capabilities

    assembled, _ = assemble_requirement(
        SourceIssue(
            key="TT2-85",
            summary="Proof of delivery capture",
            description="Capture a photo of the drop-off and collect the recipient's signature.",
        )
    )
    assert "## Stated requirement" in assembled, "the heading is still written for the model"
    assert not any(c.lstrip().startswith("#") for c in split_capabilities(assembled))


def test_instructions_to_the_agent_are_not_requirements() -> None:
    source = requested(
        "Aetherion please break this down. Capture a photo of the drop-off "
        "and collect the recipient's signature on the mobile app."
    )
    text, _ = assemble_requirement(source)
    assert "please break this down" not in text
    assert "Capture a photo of the drop-off" in text


def test_a_genuine_requirement_naming_the_keyword_survives() -> None:
    from src.context.ingest import strip_agent_instructions

    stated = "Aetherion must authenticate against the fleet API before syncing."
    assert strip_agent_instructions(stated, "Aetherion") == stated


def test_an_all_instruction_description_is_left_alone() -> None:
    from src.context.ingest import strip_agent_instructions

    only = "Aetherion please break this down."
    assert strip_agent_instructions(only, "Aetherion") == only


def test_stripping_respects_a_custom_keyword(monkeypatch) -> None:
    monkeypatch.setenv("LTW_TRIGGER_KEYWORD", "Jarvis")
    source = requested("Jarvis please create the tickets. Track fuel spend per vehicle.")
    text, _ = assemble_requirement(source)
    assert "please create the tickets" not in text
    assert "Track fuel spend per vehicle." in text


def test_context_sections_are_kept_out_of_the_heuristic_breakdown() -> None:
    """A comment must never become a Story.

    The model is told in the heading that discussion is only context; the
    heuristic fallback cannot read that, so it gets the requirement sections
    alone. Found live on TT2-107, where a comment became its own Story.
    """
    from src.classification.decompose import heuristic_breakdown
    from src.classification.validate import ProjectContext

    source = SourceIssue(
        key="TT2-107",
        summary="Vehicle inspection checklist",
        description=(
            "Let drivers complete a pre-trip vehicle inspection checklist in the "
            "mobile app, and block the route from starting until it is submitted."
        ),
        comments=[CommentInfo(author="Ann", body="Nice to have. Also add a dark mode toggle.")],
        linked_issues=[LinkedIssue(key="TT2-5", summary="Assign a job", relationship="blocks")],
    )
    text, _ = assemble_requirement(source)
    breakdown = heuristic_breakdown(text, ProjectContext(name="FleetLink"))

    titles = " | ".join(story.title for story in breakdown.stories)
    assert "dark mode" not in titles.lower(), "a comment must not become a Story"
    assert "TT2-5" not in titles, "a linked issue must not be recreated"
    assert "##" not in breakdown.epic.jira_summary
    assert "##" not in breakdown.epic.business_objective
    assert len(breakdown.stories) == 2


def test_requirement_core_keeps_the_requirement_and_attachments() -> None:
    from src.context.ingest import requirement_core

    source = SourceIssue(
        key="TT2-1",
        summary="x",
        description="Track fuel spend per vehicle.",
        attachments=[AttachmentText(filename="rates.csv", text="diesel,1.62")],
        comments=[CommentInfo(author="Ann", body="Ignore this line.")],
        worklogs=[WorklogEntry(author="Ravi", time_spent="2h")],
    )
    core = requirement_core(assemble_requirement(source)[0])
    assert "Track fuel spend per vehicle." in core
    assert "diesel,1.62" in core, "attachments state requirements and are kept"
    assert "Ignore this line." not in core
    assert "Work logged so far" not in core


def test_unsectioned_text_passes_through_requirement_core() -> None:
    from src.context.ingest import requirement_core

    plain = "Track fuel spend per vehicle."
    assert requirement_core(plain) == plain


# --- the @mention form ------------------------------------------------------
# People address the agent like a colleague: "@Aetherion Create a login
# feature." The sentence IS the requirement, so it must be kept — but the
# mention itself must not survive into the Story title.


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (
            "@Aetherion Create a login feature for all users.",
            "Create a login feature for all users.",
        ),
        (
            "Aetherion: Create a login feature for all users.",
            "Create a login feature for all users.",
        ),
        (
            "Aetherion - Create a login feature for all users.",
            "Create a login feature for all users.",
        ),
        ("Let drivers log a rest break. @Aetherion", "Let drivers log a rest break."),
    ],
)
def test_the_mention_is_removed_but_the_requirement_is_kept(
    description: str, expected: str
) -> None:
    from src.context.ingest import strip_agent_instructions

    assert strip_agent_instructions(description, "Aetherion") == expected


def test_a_bare_keyword_used_as_a_subject_is_not_stripped() -> None:
    from src.context.ingest import strip_agent_instructions

    stated = "Aetherion must authenticate against the fleet API before syncing."
    assert strip_agent_instructions(stated, "Aetherion") == stated


def test_a_mention_never_reaches_a_story_title() -> None:
    from src.classification.decompose import heuristic_breakdown
    from src.classification.validate import ProjectContext

    source = SourceIssue(
        key="TT2-200",
        summary="Login feature",
        description="@Aetherion Create a login feature for all users.",
    )
    text, _ = assemble_requirement(source)
    breakdown = heuristic_breakdown(text, ProjectContext(name="FleetLink"))

    assert breakdown.classification.value == "Small"
    title = breakdown.stories[0].title
    assert "Aetherion" not in title
    assert "@" not in title
    assert title == "Create a login feature for all users"


def test_mention_stripping_respects_a_custom_keyword(monkeypatch) -> None:
    monkeypatch.setenv("LTW_TRIGGER_KEYWORD", "Jarvis")
    source = requested("@Jarvis Track fuel spend per vehicle.")
    text, _ = assemble_requirement(source)
    assert "Jarvis" not in text.split("## Stated requirement", 1)[1].split("\n", 1)[1]
    assert "Track fuel spend per vehicle." in text


# --- where the stories should go --------------------------------------------
# The requester says this in prose, so it has to be read out of the text and
# then removed from it — otherwise "Add the stories to the current sprint"
# becomes a Story of its own.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Create a login feature.", "backlog"),
        ("Create a login feature. Add the stories to the current sprint.", "current_sprint"),
        ("Create a login feature. Put these in the current sprint please.", "current_sprint"),
        ("Create a login feature. Move them into the active sprint.", "current_sprint"),
        ("Create a login feature. Add them to the product backlog.", "backlog"),
        ("Build a sprint planning board for managers.", "backlog"),
    ],
)
def test_placement_is_read_from_the_request(text: str, expected: str) -> None:
    from src.context.ingest import detect_placement

    assert detect_placement(text)[0] == expected


def test_the_placement_instruction_is_removed_from_the_requirement() -> None:
    from src.context.ingest import detect_placement

    placement, cleaned = detect_placement(
        "Create a login feature with password reset. Add the stories to the current sprint."
    )
    assert placement == "current_sprint"
    assert cleaned == "Create a login feature with password reset."
    assert "sprint" not in cleaned.lower()


def test_a_requirement_about_sprints_is_not_a_placement_instruction() -> None:
    from src.context.ingest import detect_placement

    text = "Let managers build a sprint planning board and see sprint capacity."
    placement, cleaned = detect_placement(text)
    assert placement == "backlog"
    assert cleaned == text, "a genuine requirement must not be trimmed"


def test_a_placement_only_request_keeps_its_text() -> None:
    from src.context.ingest import detect_placement

    placement, cleaned = detect_placement("Add the stories to the current sprint.")
    assert placement == "current_sprint"
    assert cleaned, "removing everything would leave no requirement at all"


# --- the requirement arrives in a COMMENT -----------------------------------
# A request is made by commenting "@Aetherion ..." on any ticket. The comment is
# the requirement; the ticket it sits on is background. Every cleaning step has
# to apply to it — the mention once reached the title of every ticket the agent
# created (TT2-140 through TT2-152 in project TT2).


def requested(request: str) -> SourceIssue:
    """A ticket with a request made by commenting on it."""
    return SourceIssue(
        key="TT2-139",
        summary="Customer login",
        description="",
        trigger_comment_id="9001",
        trigger_comment_author="Priya",
        trigger_comment_body=request,
    )


def test_a_mention_in_the_request_never_reaches_a_story_title() -> None:
    from src.classification.decompose import heuristic_breakdown
    from src.classification.validate import ProjectContext

    source = requested(
        "@Aetherion Create a login feature for all users, including email and "
        "password authentication, password reset, and logout."
    )
    text, _ = assemble_requirement(source)
    breakdown = heuristic_breakdown(text, ProjectContext(name="FleetLink"))

    assert "@Aetherion" not in text.split("\n", 1)[1]
    assert breakdown.epic is not None
    assert "Aetherion" not in breakdown.epic.jira_summary
    for story in breakdown.stories:
        assert "Aetherion" not in story.title, story.title
        assert "Aetherion" not in story.user_story_statement
        for subtask in story.subtasks:
            assert "Aetherion" not in subtask.title, subtask.title


def test_agent_instructions_in_the_request_are_dropped() -> None:
    text, _ = assemble_requirement(
        requested("@Aetherion please break this down. Let drivers log a rest break.")
    )
    body = text.split("\n", 1)[1]
    assert "please break this down" not in body
    assert "Let drivers log a rest break." in body


def test_a_placement_instruction_in_the_request_is_not_work() -> None:
    text, _ = assemble_requirement(
        requested("@Aetherion Let drivers log a rest break. Add the stories to the current sprint.")
    )
    body = text.split("\n", 1)[1]
    assert "current sprint" not in body.lower()
    assert "Let drivers log a rest break." in body


def test_the_heading_does_not_carry_the_mention_either() -> None:
    text, _ = assemble_requirement(requested("@Aetherion Create a login feature."))
    heading = text.split("\n", 1)[0]
    assert "@Aetherion" not in heading


def test_the_comment_wins_over_the_tickets_own_description() -> None:
    """The ticket says what it is about; the comment says what is being asked."""
    source = SourceIssue(
        key="TT2-1",
        summary="Login screen",
        description="This ticket tracks login work in general.",
        trigger_comment_body="@Aetherion Let managers see total break time per driver.",
        trigger_comment_author="Priya",
        trigger_comment_id="9001",
    )
    text = assemble_requirement(source)[0]
    request = text.split("\n", 1)[1].split("##", 1)[0]
    assert "Let managers see total break time per driver." in request
    assert "tracks login work" not in request


# --- a question is not an instruction ---------------------------------------
# Live defect: "@Aetherion please explain me this ticket properly" created
# Sub-tasks. "this ticket" alone used to make a comment a pointer, so a request
# for an explanation was classified identically to "break this down into
# stories" — and answering it wrote to Jira.


@pytest.mark.parametrize(
    "comment",
    [
        "@Aetherion please explain me this ticket properly",
        "@Aetherion what does this ticket mean?",
        "@Aetherion summarise this ticket",
        "@Aetherion describe the current issue",
        "@Aetherion can you explain this issue to me",
    ],
)
def test_a_question_about_the_ticket_is_not_an_instruction_to_decompose(comment: str) -> None:
    assert is_pointer_comment(comment) is False


@pytest.mark.parametrize(
    "comment",
    [
        "@Aetherion please break this down into stories",
        "@Aetherion please break this ticket down into stories",
        "@Aetherion please add more description and create some more sub tasks",
        "@Aetherion create the sub-tasks for this ticket",
        "@Aetherion decompose this issue",
        "@Aetherion as described above",
        "@Aetherion from the description",
    ],
)
def test_asking_for_work_on_the_ticket_is_still_a_pointer(comment: str) -> None:
    """The counterweight: narrowing the rule must not lose real instructions."""
    assert is_pointer_comment(comment) is True


def test_product_language_still_outranks_a_pointer_phrase() -> None:
    assert is_pointer_comment("@Aetherion Allow drivers to log a break.") is False
    assert (
        is_pointer_comment("@Aetherion please break this down. Allow drivers to log a break.")
        is False
    )


# --- answering a question instead of decomposing ----------------------------


@pytest.mark.parametrize(
    "comment",
    [
        "@Aetherion please explain me this ticket properly",
        "@Aetherion what does this ticket mean?",
        "@Aetherion what is this ticket about?",
        "@Aetherion summarise this ticket",
        "@Aetherion describe the current issue",
        "@Aetherion tell me what this is about",
        "@Aetherion give me a summary",
    ],
)
def test_a_request_to_be_told_something_is_a_question(comment: str) -> None:
    assert is_question_comment(comment) is True


@pytest.mark.parametrize(
    "comment",
    [
        "@Aetherion please break this down into stories",
        "@Aetherion Allow drivers to log a break.",
        # Product language wins, exactly as it does for pointer comments.
        "@Aetherion Explain how drivers log a break, and let dispatchers see it.",
        # Bare "what" used to match here, making this a question.
        "@Aetherion Add a what-if calculator for fuel spend.",
        "@Aetherion Allow users to choose what happens on failure.",
    ],
)
def test_a_request_for_work_is_not_a_question(comment: str) -> None:
    assert is_question_comment(comment) is False


def test_the_answer_describes_the_ticket_without_inventing_anything() -> None:
    source = SourceIssue(
        key="FL-53",
        summary="Fault reporting",
        issue_type="Task",
        status="To Do",
        reporter="Laxman",
        description="Allow drivers to report a vehicle fault from their phone.",
        trigger_comment_id="1",
        trigger_comment_author="Laxman",
        trigger_comment_body="@Aetherion please explain me this ticket properly",
    )
    answer = describe_issue(source)
    assert "FL-53 is a Task, currently To Do, raised by Laxman." in answer
    assert '"Fault reporting"' in answer
    assert "Allow drivers to report a vehicle fault from their phone." in answer
    assert "Nothing was created" in answer
    # Every fact traces to a field of the issue; no numbers are introduced.
    assert "2 " not in answer.replace("FL-53", "")


def test_an_issue_with_no_description_says_so_rather_than_guessing() -> None:
    answer = describe_issue(SourceIssue(key="FL-1", summary="Placeholder", issue_type="Task"))
    assert "no description" in answer


# --- the ticket and its linked page disagreeing -----------------------------
# AUDIT D5: the ticket said "one saved card", the newer linked page said "five",
# and the ticket won silently. Nothing here decides which is right; reporting
# the disagreement is the whole point.


def _payments(description: str, page_text: str) -> SourceIssue:
    from src.models.schemas import ConfluencePage

    return SourceIssue(
        key="FL-70",
        summary="Saved cards",
        description=description,
        confluence_pages=[
            ConfluencePage(page_id="1", title="Payments Spec", url="u", text=page_text)
        ],
    )


def test_a_number_the_page_contradicts_is_reported() -> None:
    from src.context.ingest import find_conflicts

    (conflict,) = find_conflicts(
        _payments(
            "Customers may store one saved card per customer.",
            "Customers may store up to five saved cards.",
        )
    )
    assert "1 card" in conflict and "5" in conflict
    assert "Payments Spec" in conflict
    assert "The ticket was used" in conflict, "it must say which one it acted on"


@pytest.mark.parametrize(
    "description, page_text",
    [
        # The same claim, worded differently.
        ("Customers may store one saved card.", "Customers may store a single saved card."),
        # Different subjects entirely.
        ("Customers may store one saved card.", "Refunds take three days."),
        # Nothing countable in the ticket to contradict.
        ("Let customers save a payment method.", "Up to five saved cards."),
    ],
)
def test_agreement_and_unrelated_numbers_are_not_reported(description, page_text) -> None:
    from src.context.ingest import find_conflicts

    assert find_conflicts(_payments(description, page_text)) == []


def test_an_unreadable_page_cannot_contradict_anything() -> None:
    from src.context.ingest import find_conflicts
    from src.models.schemas import ConfluencePage

    source = SourceIssue(
        key="FL-70",
        description="Customers may store one saved card.",
        confluence_pages=[ConfluencePage(page_id="1", title="X", url="u", text="", note="404")],
    )
    assert find_conflicts(source) == []


def test_the_thing_being_counted_is_the_head_noun() -> None:
    """ "one saved card per customer" counts cards, not customers."""
    from src.context.ingest import _quantities

    assert _quantities("Customers may store one saved card per customer.") == {"card": {1}}
    assert _quantities("Customers may store up to five saved cards.") == {"card": {5}}


def test_a_contradicting_page_is_left_out_of_the_work_items() -> None:
    """Found live on FL-94.

    The reply said "The ticket was used", but the page text was merged in
    anyway, producing sub-tasks reading "...one saved card per customer and
    Customers may store up to five saved cards" — the contradiction baked into
    the work item. Saying the ticket was used has to be true.
    """
    from src.classification.decompose import heuristic_breakdown
    from src.classification.validate import ProjectContext
    from src.context.ingest import assemble_requirement

    source = _payments(
        "Customers may store one saved card per customer.",
        "Customers may store up to five saved cards.",
    )
    requirement, _ = assemble_requirement(source)
    breakdown = heuristic_breakdown(requirement, ProjectContext())

    rendered = json.dumps(breakdown.model_dump(mode="json")).lower()
    assert "five" not in rendered and " 5 " not in rendered
    assert len(breakdown.stories) == 1
    assert "one saved card" in breakdown.stories[0].title.lower()


def test_a_page_that_agrees_is_still_used() -> None:
    """The counterweight: only a contradicting page is dropped."""
    from src.classification.decompose import heuristic_breakdown
    from src.classification.validate import ProjectContext
    from src.context.ingest import assemble_requirement

    source = _payments(
        "Customers may store one saved card.",
        "Customers may also see a receipt for every payment.",
    )
    requirement, _ = assemble_requirement(source)
    breakdown = heuristic_breakdown(requirement, ProjectContext())
    assert len(breakdown.stories) == 2, [s.title for s in breakdown.stories]
    assert any("receipt" in s.title.lower() for s in breakdown.stories)


# --- a question must be about the ticket ------------------------------------
# Found live: "@Aetherion What is the weather today?" was answered with a polite
# description of the ticket. The question gate runs before validation, so
# anything it claims never reaches the out-of-scope check.


@pytest.mark.parametrize(
    "comment",
    [
        "@Aetherion What is the weather today?",
        "@Aetherion tell me a joke",
        "@Aetherion how are you?",
        "@Aetherion what time is it?",
        "@Aetherion who are you?",
    ],
)
def test_small_talk_is_not_a_question_about_the_ticket(comment: str) -> None:
    assert is_question_comment(comment) is False


# --- a request that is only an unreadable link ------------------------------
# Found live: "@Aetherion Build what this says: <youtube link>". The link was
# correctly never fetched, strip_urls removed it, and the four words left
# ("Build what this says") passed validation on the word "build" and were then
# matched against an existing ticket at 0.75 similarity.


def _linked(comment: str, pages=None) -> SourceIssue:
    return SourceIssue(
        key="FL-94",
        summary="Saved cards",
        description="Saved cards.",
        confluence_pages=pages or [],
        trigger_comment_id="1",
        trigger_comment_author="You",
        trigger_comment_body=comment,
    )


@pytest.mark.parametrize(
    "comment",
    [
        "@Aetherion Build what this says: https://www.youtube.com/watch?v=abc",
        "@Aetherion Build what this spec says: https://other.atlassian.net/wiki/spaces/X/pages/1/Y",
        "@Aetherion see https://example.com/spec",
    ],
)
def test_a_request_that_is_only_an_unreadable_link_asks(comment: str) -> None:
    from src.context.ingest import find_unread_links, request_is_only_a_pointer

    assert request_is_only_a_pointer(_linked(comment)) is True
    assert find_unread_links(_linked(comment)), "the ignored link must be nameable"


def test_a_link_that_was_read_is_not_treated_as_missing() -> None:
    from src.context.ingest import find_unread_links, request_is_only_a_pointer
    from src.models.schemas import ConfluencePage

    url = "https://site.atlassian.net/wiki/spaces/FL/pages/1/Spec"
    pages = [
        ConfluencePage(page_id="1", title="Spec", url=url, text="Allow drivers to log a break.")
    ]
    source = _linked(f"@Aetherion Build what this says: {url}", pages)
    assert find_unread_links(source) == []
    assert request_is_only_a_pointer(source) is False


def test_a_real_requirement_carrying_a_link_is_not_blocked() -> None:
    """The counterweight: a link alongside a stated requirement changes nothing."""
    from src.context.ingest import request_is_only_a_pointer

    source = _linked(
        "@Aetherion Allow drivers to log a rest break, see https://example.com/notes "
        "for background."
    )
    assert request_is_only_a_pointer(source) is False
