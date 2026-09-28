"""A comment that is only "@Aetherion build" means: break THIS ticket down.

BGV-3 (2026-09-24): a Story with a clear six-line description, commented
"@Aetherion build". The one word "build" was judged as the requirement and the
reply was "Invalid request — this is not a work requirement"; the description
was never looked at.
"""

from __future__ import annotations

import pytest

from src.classification.request import RequestBucket, classify_request
from src.classification.validate import ProjectContext
from src.context.ingest import assemble_requirement, is_pointer_comment, requirement_core
from src.models.schemas import SourceIssue

BGV3_DESCRIPTION = (
    "Before any background check starts, the candidate must give digital consent.\n"
    "- Candidate receives a consent link by email\n"
    '- Candidate reads the consent text and ticks "I agree"\n'
    "- Consent is stored with date, time and IP address\n"
    "- Recruiter can see consent status (Pending / Given / Declined) on the candidate profile\n"
    "- Verification cannot start until consent is Given"
)


@pytest.mark.parametrize(
    "comment",
    [
        "@Aetherion build",
        "@Aetherion build.",
        "@Aetherion Build this",
        "@Aetherion build this ticket please",
        "@Aetherion please decompose",
        "@Aetherion breakdown",
        "@Aetherion break down",
        "@Aetherion create tickets",
        # BGV-3's second attempt, typo included.
        "@Aetherion build based on the descrption",
        "@Aetherion build according to the description",
        "@Aetherion build as per the ticket",
        "@Aetherion build using the description",
        "@Aetherion build based on the give description",
    ],
)
def test_a_bare_build_verb_points_at_the_ticket(comment: str) -> None:
    assert is_pointer_comment(comment)


@pytest.mark.parametrize(
    "comment",
    [
        # A verb followed by a requirement: the comment IS the work.
        "@Aetherion build let recruiters send reminder emails to referees",
        # Nothing asked at all stays "nothing asked" — never silently decompose.
        "@Aetherion",
        # A question is not an instruction to build.
        "@Aetherion explain this",
    ],
)
def test_anything_more_than_a_bare_verb_is_not_this_pointer(comment: str) -> None:
    assert not is_pointer_comment(comment)


def test_bgv3_is_judged_on_its_description_not_the_word_build() -> None:
    source = SourceIssue(
        key="BGV-3",
        summary="Candidate consent before verification",
        description=BGV3_DESCRIPTION,
        trigger_comment_id="1",
        trigger_comment_author="Laxman",
        trigger_comment_body="@Aetherion build",
    )
    text, _ = assemble_requirement(source)
    stated = requirement_core(text)
    assert "digital consent" in stated
    assert "\nbuild\n" not in f"\n{stated}\n"

    bucket, _ = classify_request(stated, ProjectContext())
    assert bucket is not RequestBucket.NOT_A_REQUIREMENT


def test_the_ticket_being_built_is_not_listed_as_existing_work() -> None:
    """BGV-3 was asked "does this differ from BGV-3?" by the model."""
    from src.tools.tools import _context_from

    context = _context_from(
        {
            "project": {"key": "BGV", "name": "Praman"},
            "existing_issues": [
                {"key": "BGV-3", "summary": "Candidate consent before verification"},
                {"key": "BGV-1", "summary": "Agent test ticket"},
            ],
            "trigger_key": "BGV-3",
        }
    )
    assert context.existing_titles == ("BGV-1 — Agent test ticket",)
