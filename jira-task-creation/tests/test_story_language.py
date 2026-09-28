"""The wording of a generated Story.

These read as cosmetic and are not: every Story the agent writes carries this
phrasing into a real backlog, and "As a operations manager" in forty tickets is
what people notice first.
"""

from __future__ import annotations

import pytest

from src.classification.decompose import _article_for, heuristic_breakdown
from src.classification.validate import ProjectContext

CTX = ProjectContext(
    name="FleetLink",
    description="A delivery fleet platform. Dispatchers assign jobs to drivers.",
)


# --- a / an, chosen by sound ------------------------------------------------


@pytest.mark.parametrize(
    ("noun", "expected"),
    [
        ("operations manager", "an"),
        ("admin", "an"),
        ("engineer", "an"),
        ("owner", "an"),
        ("employee", "an"),
        ("driver", "a"),
        ("dispatcher", "a"),
        ("tenant", "a"),
        # Vowel letter, consonant sound — the case plain matching gets wrong.
        ("user of this product", "a"),
        ("unit manager", "a"),
        ("European partner", "a"),
        # Consonant letter, vowel sound.
        ("hour-long shift", "an"),
        ("honest broker", "an"),
        ("", "a"),
    ],
)
def test_the_article_follows_the_sound_not_the_spelling(noun: str, expected: str) -> None:
    assert _article_for(noun) == expected


def test_no_story_says_a_operations_manager() -> None:
    breakdown = heuristic_breakdown(
        "Allow operations managers to see the total fuel cost for a vehicle.", CTX
    )
    statement = breakdown.stories[0].user_story_statement
    assert statement.startswith("As an operations manager,"), statement


# --- the actor carries across clauses ---------------------------------------


def test_an_actor_named_once_carries_to_later_clauses() -> None:
    """A reader carries "dispatchers" across the list; so should the agent."""
    breakdown = heuristic_breakdown(
        "Let dispatchers assign a job to a driver, allow drivers to accept or "
        "decline a job, and notify the dispatcher when a job is declined.",
        CTX,
    )
    statements = [s.user_story_statement for s in breakdown.stories]
    assert len(statements) == 3
    # The third clause names no actor of its own, and must not fall back to the
    # generic when the requirement already told us who is involved.
    assert "user of this product" not in statements[2], statements[2]


def test_the_generic_actor_survives_when_nobody_is_named() -> None:
    """Honest fallback: invent nothing when the requirement names no one."""
    breakdown = heuristic_breakdown(
        "Track vehicle servicing, tyre replacement, and insurance renewal.", CTX
    )
    assert all("user of this product" in s.user_story_statement for s in breakdown.stories)


def test_every_statement_keeps_the_canonical_shape() -> None:
    breakdown = heuristic_breakdown(
        "Let dispatchers assign a job to a driver, and notify the dispatcher when "
        "a job is declined.",
        CTX,
    )
    for story in breakdown.stories:
        statement = story.user_story_statement
        assert statement.startswith(("As a ", "As an ")), statement
        assert ", I want to " in statement, statement
        assert ", so that " in statement, statement
        assert statement.endswith("."), statement
