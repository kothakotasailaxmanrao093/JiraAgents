"""The FL-27 failures, pinned.

A comment on FL-27 produced twelve tickets that should never have existed. Three
separate mistakes, each with its own test here so none of them returns.
"""

from __future__ import annotations

import pytest

from src.classification.decompose import heuristic_breakdown, split_capabilities
from src.classification.validate import ProjectContext
from src.context.ingest import assemble_requirement, is_pointer_comment
from src.models.schemas import SourceIssue

CTX = ProjectContext(
    name="FleetLinkTest",
    description="A delivery fleet platform. Dispatchers assign jobs to drivers.",
)


# --- mistake 1: an instruction was built as if it were a requirement --------
# "@Aetherion add more descrption and create some more sub tasks" is a message
# about the ticket. It became Epic FL-28 and Story FL-29 "Add more descrption".


@pytest.mark.parametrize(
    "comment",
    [
        "add more descrption and create some more sub tasks",
        "add more descrption and create some more sub tasks reference FL-27",
        "break this down",
        "please break this ticket down into stories",
        "create some more subtasks",
    ],
)
def test_an_instruction_about_the_ticket_is_a_pointer(comment: str) -> None:
    assert is_pointer_comment(comment)


@pytest.mark.parametrize(
    "comment",
    [
        "Allow drivers to upload a photo of a delivered parcel.",
        "Create me dashboard with all the drivers and vehicle.",
        # Mentions tickets, but is plainly product work.
        "Let dispatchers assign a job, and create tickets for each step.",
        # Contains the words "add more description" inside a real requirement.
        "Allow users to add more description to their profile.",
        # An instruction AND a requirement — the requirement must win, or the
        # work being asked for is silently thrown away.
        "please break this down. Capture a photo of the drop-off and collect "
        "the recipient's signature on the mobile app.",
    ],
)
def test_a_real_requirement_is_never_mistaken_for_a_pointer(comment: str) -> None:
    assert not is_pointer_comment(comment)


def test_a_pointer_comment_decomposes_the_tickets_own_description() -> None:
    """Exactly FL-27: the ticket said what was wanted; the comment just asked."""
    source = SourceIssue(
        key="FL-27",
        summary="Create me a dashboard",
        description="Create me dashboard with all the drivers and vehicle, with all the tracker.",
        trigger_comment_id="1",
        trigger_comment_author="Laxman",
        trigger_comment_body="@Aetherion add more descrption and create some more sub tasks",
    )
    text, _ = assemble_requirement(source)
    body = text.split("\n", 1)[1]

    assert "dashboard with all the drivers" in body
    assert "add more descrption" not in body.lower()
    assert "sub tasks" not in body.lower()


# --- mistake 2: one idea split into three fragments -------------------------
# "…with all the drivers and vehicle, with all the tracker" became three
# Stories: a half sentence, an invented "Create vehicle", and "With all the
# tracker", which is not a sentence at all.


def test_a_modifier_phrase_is_not_a_separate_capability() -> None:
    caps = split_capabilities(
        "Create me dashboard with all the dirvers and vehicle, with all the tracker."
    )
    assert len(caps) == 1, caps


def test_no_story_is_built_out_of_a_fragment() -> None:
    breakdown = heuristic_breakdown(
        "Create me dashboard with all the dirvers and vehicle, with all the tracker.",
        CTX,
    )
    titles = [s.title.lower() for s in breakdown.stories]
    assert breakdown.classification.value == "Small"
    assert breakdown.epic is None
    assert not any(t.startswith("with ") for t in titles), titles
    assert "create vehicle" not in titles, titles


# --- mistake 3: fixing the above must not un-split real lists ---------------
# The first attempt at the fix merged "insurance renewal" into the clause
# before it, because "insurance".startswith("in") and "in" is a preposition.


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        (
            "Track vehicle servicing, tyre replacement, insurance renewal, fuel card "
            "usage, driver licence expiry, and accident reports.",
            6,
        ),
        (
            "Let dispatchers assign a job to a driver, allow drivers to accept or "
            "decline a job, and notify the dispatcher when a job is declined.",
            3,
        ),
        (
            "Allow customers to rate a completed delivery, and let managers see the "
            "average rating per driver.",
            2,
        ),
        # Words that merely begin with a preposition: insurance/offer/form/onboarding.
        ("Track insurance renewal, offer discounts, and form new contracts.", 3),
        ("Support onboarding, and track international shipments.", 2),
        ("Allow drivers to upload a photo of a delivered parcel.", 1),
    ],
)
def test_real_lists_still_split(requirement: str, expected: int) -> None:
    caps = split_capabilities(requirement)
    assert len(caps) == expected, caps


def test_a_deliberate_bullet_list_is_never_merged() -> None:
    """Each bullet is a capability by definition, whatever it looks like."""
    caps = split_capabilities("- fuel spend by depot\n- with all the trackers\n- insurance renewal")
    assert len(caps) == 3, caps


# --- mistake 4: a duplicate Story instead of the ticket that was pointed at --
# On FL-15 the agent created Story FL-16 saying the same thing as FL-15, and
# hung FL-17/18/19 off the copy. FL-15 — the ticket that was asked about —
# ended up with no sub-tasks at all.


def test_a_pointer_request_marks_itself_as_one() -> None:
    from src.context.ingest import is_pointer_comment

    assert is_pointer_comment("add more descrption and create some more sub tasks")


# Sub-tasks go on the ticket asked about, never on a copy of it: now the root
# behaviour, tested in test_root_ticket.py (one capability -> the ticket is the
# Story; several -> the ticket is the Epic).


# --- mistake 3: the joining words were eaten --------------------------------


def test_merging_keeps_the_words_that_joined_the_fragments() -> None:
    """ "…drivers vehicle with all the tracker" was not a sentence any more."""
    caps = split_capabilities(
        "Create me dashboard with all the dirvers and vehicle, with all the tracker."
    )
    assert len(caps) == 1
    assert "dirvers and vehicle" in caps[0], caps[0]
    assert "vehicle, with all the tracker" in caps[0], caps[0]


def test_the_story_and_its_subtasks_all_read_properly() -> None:
    breakdown = heuristic_breakdown(
        "Create me dashboard with all the dirvers and vehicle, with all the tracker.",
        CTX,
    )
    story = breakdown.stories[0]
    for text in [story.title, *(s.title for s in story.subtasks)]:
        assert "dirvers and vehicle" in text, text
